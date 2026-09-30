import httpx
import pytest

from app.integrations.errors import IntegrationError
from app.integrations.media import MediaDownloader

ALLOWED = (
    "https://blipmediastore.blob.core.windows.net/thread-medias/Media_1_abc.pdf?sv=1&sig=SECRET"
)


def _downloader(settings, handler) -> tuple[MediaDownloader, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MediaDownloader(settings, client), client


@pytest.mark.asyncio
async def test_allowed_media_is_downloaded_with_declared_type_and_hinted_name(settings) -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "text/plain"})

    downloader, client = _downloader(settings, handler)
    media = await downloader.download(ALLOWED, content_type="application/pdf", filename="Deal.pdf")
    await client.aclose()

    assert requested == [ALLOWED]
    assert media is not None
    assert (media.filename, media.content_type, media.data) == (
        "Deal.pdf",
        "application/pdf",
        b"%PDF-1.7",
    )


@pytest.mark.asyncio
async def test_missing_metadata_falls_back_to_served_type_and_url_name(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png; charset=x"})

    downloader, client = _downloader(settings, handler)
    media = await downloader.download(ALLOWED.replace("Media_1_abc.pdf", "my%20pic.png"))
    await client.aclose()

    assert media is not None
    assert (media.filename, media.content_type) == ("my pic.png", "image/png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "http://blipmediastore.blob.core.windows.net/a.pdf",
        "https://evil.example/a.pdf",
        "https://blipmediastore.blob.core.windows.net.evil.example/a.pdf",
        "https://user@blipmediastore.blob.core.windows.net/a.pdf",
        "https://blipmediastore.blob.core.windows.net:8443/a.pdf",
        "https://169.254.169.254/latest/meta-data",
        "not a url",
    ],
)
async def test_hosts_outside_the_allow_list_are_never_requested(settings, uri) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    downloader, client = _downloader(settings, handler)

    assert await downloader.download(uri) is None
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 403, 404, 409])
async def test_expired_or_moved_media_is_skipped(settings, status) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"location": "https://evil.example/"})

    downloader, client = _downloader(settings, handler)

    assert await downloader.download(ALLOWED) is None
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_side_failures_are_retryable(settings, status) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    downloader, client = _downloader(settings, handler)
    with pytest.raises(IntegrationError) as error:
        await downloader.download(ALLOWED)
    await client.aclose()

    assert error.value.retryable is True
    assert "SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_timeouts_are_retryable(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    downloader, client = _downloader(settings, handler)
    with pytest.raises(IntegrationError) as error:
        await downloader.download(ALLOWED)
    await client.aclose()

    assert error.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("declares_length", [True, False])
async def test_files_over_the_limit_are_skipped(settings, declares_length) -> None:
    settings.blip_media_max_bytes = 10

    async def handler(request: httpx.Request) -> httpx.Response:
        stream = httpx.ByteStream(b"x" * 11)
        headers = {"content-length": "11"} if declares_length else {}
        return httpx.Response(200, headers=headers, stream=stream)

    downloader, client = _downloader(settings, handler)

    assert await downloader.download(ALLOWED) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_files_are_skipped(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    downloader, client = _downloader(settings, handler)

    assert await downloader.download(ALLOWED) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_unsafe_filenames_are_sanitised(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    downloader, client = _downloader(settings, handler)
    media = await downloader.download(ALLOWED, filename="../../etc/passwd\x00.pdf")
    await client.aclose()

    assert media is not None
    assert "/" not in media.filename and "\x00" not in media.filename
    assert media.content_type == "application/octet-stream"


def test_allowed_hosts_setting_is_parsed_from_a_comma_separated_list(settings) -> None:
    settings.blip_media_allowed_hosts = " A.example , ,b.example"

    assert settings.blip_media_hosts == frozenset({"a.example", "b.example"})
