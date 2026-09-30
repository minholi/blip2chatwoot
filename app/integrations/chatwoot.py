from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.integrations.errors import IntegrationError


class ChatwootClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_contact(
        self,
        *,
        name: str,
        identifier: str,
        phone_number: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "inbox_id": self.settings.chatwoot_inbox_id,
            "name": name,
            "identifier": identifier,
            "additional_attributes": {"blip_identity": identifier},
        }
        if phone_number:
            payload["phone_number"] = phone_number
        return await self._request(
            "POST",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/contacts",
            json=payload,
        )

    async def get_contact(self, *, contact_id: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/contacts/{contact_id}",
        )

    async def update_contact(
        self,
        *,
        contact_id: int,
        name: str | None = None,
        email: str | None = None,
        custom_attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields = {"name": name, "email": email, "custom_attributes": custom_attributes}
        return await self._request(
            "PUT",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/contacts/{contact_id}",
            json={key: value for key, value in fields.items() if value is not None},
        )

    async def create_conversation(self, *, source_id: str, contact_id: int) -> dict[str, Any]:
        payload = {
            "source_id": source_id,
            "inbox_id": self.settings.chatwoot_inbox_id,
            "contact_id": contact_id,
            "status": "open",
        }
        return await self._request(
            "POST",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations",
            json=payload,
        )

    async def create_message(
        self,
        *,
        conversation_id: int,
        content: str,
        message_type: str,
        private: bool = False,
        content_attributes: dict[str, Any] | None = None,
        as_agent_bot: bool = False,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Post a message; ``attachments`` are ``(filename, data, content type)`` files."""
        path = (
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/messages"
        )
        api_token = self.settings.chatwoot_agent_bot_token if as_agent_bot else None
        if attachments:
            fields = {
                "content": content,
                "message_type": message_type,
                "private": "true" if private else "false",
            }
            for key, value in (content_attributes or {}).items():
                fields[f"content_attributes[{key}]"] = str(value)
            return await self._request(
                "POST",
                path,
                api_token=api_token,
                data=fields,
                files=[
                    ("attachments[]", (filename, data, content_type))
                    for filename, data, content_type in attachments
                ],
            )
        payload = {
            "content": content,
            "message_type": message_type,
            "private": private,
        }
        if content_attributes:
            payload["content_attributes"] = content_attributes
        return await self._request("POST", path, api_token=api_token, json=payload)

    async def get_messages(self, *, conversation_id: int) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/messages",
        )
        messages = response.get("payload") or response.get("messages") or []
        if isinstance(messages, dict):
            messages = messages.get("messages") or []
        return [item for item in messages if isinstance(item, dict)]

    async def toggle_status(self, *, conversation_id: int, status: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/toggle_status",
            json={"status": status},
        )

    async def update_message_status(
        self,
        *,
        conversation_id: int,
        message_id: int,
        status: str,
        external_error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status}
        if external_error:
            payload["external_error"] = external_error
        return await self._request(
            "PATCH",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/messages/{message_id}",
            json=payload,
        )

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        response = await self._request(
            "GET",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/labels",
        )
        return [str(label) for label in response.get("payload", [])]

    async def add_conversation_labels(
        self,
        *,
        conversation_id: int,
        labels: list[str],
    ) -> list[str]:
        response = await self._request(
            "POST",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/conversations/"
            f"{conversation_id}/labels",
            json={"labels": labels},
        )
        return [str(label) for label in response.get("payload", labels)]

    async def list_labels(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/labels",
        )
        return [item for item in response.get("payload", []) if isinstance(item, dict)]

    async def create_label(self, title: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/accounts/{self.settings.chatwoot_account_id}/labels",
            json={"title": title, "color": "#1f93ff", "show_on_sidebar": True},
        )

    async def ensure_labels(self, labels: list[str]) -> None:
        if not labels:
            return
        existing = {str(item.get("title")) for item in await self.list_labels()}
        for label in labels:
            if label not in existing:
                try:
                    await self.create_label(label)
                except IntegrationError as exc:
                    if exc.status_code != 422:
                        raise

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api_token: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        token = api_token or self.settings.chatwoot_api_token
        if not self.settings.chatwoot_base_url or not token:
            raise IntegrationError("Chatwoot credentials are not configured", retryable=False)
        headers = kwargs.pop("headers", {})
        headers.update(
            {
                "api_access_token": token,
                "Accept": "application/json",
            }
        )
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        try:
            response = await self._client.request(
                method,
                f"{self.settings.chatwoot_base_url.rstrip('/')}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise IntegrationError(f"Chatwoot request timed out: {exc}", retryable=True) from exc
        except httpx.RequestError as exc:
            raise IntegrationError(f"Chatwoot request failed: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise IntegrationError(
                f"Chatwoot returned HTTP {response.status_code}: {response.text[:500]}",
                retryable=retryable,
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
