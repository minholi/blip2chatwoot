from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.config import Settings
from app.integrations.errors import IntegrationError


class BlipClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        self._owns_client = client is None

    @property
    def messages_url(self) -> str:
        return f"https://{self.settings.blip_contract_id}.http.msging.net/messages"

    @property
    def notifications_url(self) -> str:
        return f"https://{self.settings.blip_contract_id}.http.msging.net/notifications"

    @property
    def commands_url(self) -> str:
        return f"https://{self.settings.blip_contract_id}.http.msging.net/commands"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_message(
        self,
        *,
        to: str,
        message_type: str,
        content: Any,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": message_id or str(uuid4()),
            "to": to,
            "type": message_type,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata
        return await self._post(self.messages_url, payload)

    async def send_notification(
        self,
        *,
        message_id: str,
        to: str,
        event: str,
        reason: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": message_id, "to": to, "event": event}
        if reason:
            payload["reason"] = reason
        return await self._post(self.notifications_url, payload)

    async def command(
        self,
        *,
        method: str,
        uri: str,
        to: str = "postmaster@desk.msging.net",
        media_type: str | None = None,
        resource: Any | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(uuid4()),
            "to": to,
            "method": method.lower(),
            "uri": uri,
        }
        if media_type:
            payload["type"] = media_type
        if resource is not None:
            payload["resource"] = resource
        response = await self._post(self.commands_url, payload)
        if response.get("status") == "failure":
            reason = response.get("reason") or {}
            raise IntegrationError(
                f"BLiP command failed: {reason.get('description', 'unknown error')}",
                retryable=False,
            )
        return response

    async def create_shadow_ticket(self, customer_identity: str) -> str:
        uri_identity = quote(customer_identity, safe="@._-")
        response = await self.command(
            method="set",
            uri=f"/tickets/{uri_identity}",
            media_type="text/plain",
            resource="Chatwoot bridge",
        )
        resource = response.get("resource") or {}
        ticket_id = resource.get("id")
        if not ticket_id:
            raise IntegrationError("BLiP did not return a shadow ticket id", retryable=False)
        return str(ticket_id)

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        response = await self.command(method="get", uri=f"/ticket/{quote(ticket_id, safe='._-')}")
        return response.get("resource") or {}

    async def change_ticket_tags(self, ticket_id: str, tags: list[str]) -> dict[str, Any]:
        return await self.command(
            method="set",
            uri=f"/tickets/{quote(ticket_id, safe='._-')}/change-tags",
            media_type="application/vnd.iris.ticket+json",
            resource={"id": ticket_id, "tags": tags},
        )

    async def get_active_tags(self) -> set[str]:
        response = await self.command(method="get", uri="/tags/active")
        resource = response.get("resource") or {}
        return {
            str(item["name"])
            for item in resource.get("items", [])
            if isinstance(item, dict) and item.get("name")
        }

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.blip_contract_id or not self.settings.blip_auth_key:
            raise IntegrationError("BLiP credentials are not configured", retryable=False)
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Key {self.settings.blip_auth_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise IntegrationError(f"BLiP request timed out: {exc}", retryable=True) from exc
        except httpx.RequestError as exc:
            raise IntegrationError(f"BLiP request failed: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise IntegrationError(
                f"BLiP returned HTTP {response.status_code}: {response.text[:500]}",
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
