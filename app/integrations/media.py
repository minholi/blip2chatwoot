from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from app.config import Settings
from app.integrations.errors import IntegrationError

logger = logging.getLogger(__name__)
_MIME_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_FILENAME_UNSAFE = re.compile(r"[\x00-\x1f\\/]+")


@dataclass(frozen=True)
class DownloadedMedia:
    filename: str
    content_type: str
    data: bytes


class MediaDownloader:
    """Fetches BLiP media so Chatwoot can hold the file instead of a link.

    BLiP media links are signed URLs that stop working ~30 minutes after the message, and the BLiP
    webhook can be unauthenticated, so only allow-listed https hosts are ever fetched (no SSRF).
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self._client = client

    async def download(
        self,
        uri: str,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> DownloadedMedia | None:
        """Return the file, or None when the caller should fall back to a text link.

        A host that is not allowed, an expired link, or a file over the size limit are permanent
        (None). Timeouts, network errors and 429/5xx raise a retryable ``IntegrationError``.
        """
        if not self._is_allowed(uri):
            logger.warning("Not attaching BLiP media from %s: host not allowed", _describe(uri))
            return None
        limit = self.settings.blip_media_max_bytes
        try:
            async with self._client.stream("GET", uri, follow_redirects=False) as response:
                status = response.status_code
                if status == 429 or status >= 500:
                    raise IntegrationError(
                        f"BLiP media returned HTTP {status}", retryable=True, status_code=status
                    )
                if status != 200:
                    logger.warning("BLiP media %s unavailable: HTTP %s", _describe(uri), status)
                    return None
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > limit:
                    logger.warning("BLiP media %s is over %s bytes", _describe(uri), limit)
                    return None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        logger.warning("BLiP media %s is over %s bytes", _describe(uri), limit)
                        return None
                served_type = response.headers.get("content-type", "")
        except httpx.TimeoutException as exc:
            raise IntegrationError(f"BLiP media download timed out: {exc}", retryable=True) from exc
        except httpx.RequestError as exc:
            raise IntegrationError(f"BLiP media download failed: {exc}", retryable=True) from exc

        if not data:
            return None
        mime = _mime_type(content_type) or _mime_type(served_type) or "application/octet-stream"
        return DownloadedMedia(
            filename=_filename(filename, uri, mime), content_type=mime, data=bytes(data)
        )

    def _is_allowed(self, uri: str) -> bool:
        try:
            parts = urlsplit(uri)
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme == "https"
            and parts.username is None
            and port in (None, 443)
            and parts.hostname is not None
            and parts.hostname.lower() in self.settings.blip_media_hosts
        )


def _describe(uri: str) -> str:
    """Host and path only: the query of a signed URL is a credential and stays out of logs."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<invalid url>"
    return f"{parts.hostname}{parts.path}"


def _mime_type(value: str | None) -> str | None:
    candidate = (value or "").split(";")[0].strip().lower()
    return candidate if _MIME_TYPE.match(candidate) else None


def _filename(hint: str | None, uri: str, mime: str) -> str:
    name = hint or unquote(PurePosixPath(urlsplit(uri).path).name)
    name = _FILENAME_UNSAFE.sub("_", name).strip(" .")[:120]
    return name or f"attachment{mimetypes.guess_extension(mime) or ''}"
