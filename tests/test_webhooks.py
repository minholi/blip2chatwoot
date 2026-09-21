import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import router
from app.models import InboundEvent, OutboxJob


@pytest.mark.asyncio
async def test_blip_webhook_persists_event_once(session_factory, settings) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)
    payload = {
        "id": "message-1",
        "from": "551199999999@wa.gw.msging.net",
        "to": "mybot@msging.net",
        "type": "text/plain",
        "content": "Hello",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/blip/messages/route-secret",
            json=payload,
        )
        duplicate = await client.post(
            "/webhooks/blip/messages/route-secret",
            json=payload,
        )

    assert response.status_code == 200
    assert duplicate.json()["duplicate"] is True
    async with session_factory() as session:
        assert len((await session.scalars(select(InboundEvent))).all()) == 1
        assert len((await session.scalars(select(OutboxJob))).all()) == 1


@pytest.mark.asyncio
async def test_chatwoot_webhook_requires_valid_signature(session_factory, settings) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)
    body = json.dumps(
        {
            "event": "conversation_updated",
            "id": 200,
            "account": {"id": 10},
            "labels": ["vip"],
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    signature = (
        "sha256="
        + hmac.new(
            settings.chatwoot_webhook_secret.encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            "/webhooks/chatwoot",
            content=body,
            headers={"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": "bad"},
        )
        valid = await client.post(
            "/webhooks/chatwoot",
            content=body,
            headers={"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": signature},
        )

    assert invalid.status_code == 401
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_blip_notification_uses_its_own_path_token(session_factory, settings) -> None:
    settings.blip_notification_path_token = "notification-secret"
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)
    payload = {"id": "blip-message-1", "event": "received"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post("/webhooks/blip/notifications", json=payload)
        valid = await client.post(
            "/webhooks/blip/notifications/notification-secret",
            json=payload,
        )

    assert invalid.status_code == 401
    assert valid.status_code == 200
