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


_CONTACT_CUSTOMER = "551199999999@wa.gw.msging.net"


def _contact_app(session_factory, settings) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)
    return app


def _contact_update(**fields) -> dict:
    return {
        "identity": _CONTACT_CUSTOMER,
        "source": "WhatsApp",
        "lastMessageDate": "2026-09-29T11:00:00.000Z",
        **fields,
    }


@pytest.mark.asyncio
async def test_blip_tracking_events_are_acknowledged_without_persisting(
    session_factory,
    settings,
) -> None:
    tracking = {
        "identity": _CONTACT_CUSTOMER,
        "ownerIdentity": "mybot@msging.net",
        "category": "flow",
        "action": "Start",
        "messageId": "message-1",
        "contact": {"Identity": _CONTACT_CUSTOMER},
        "extras": {"crmId": 1},
        "storageDate": "2026-09-29T11:00:00.000Z",
    }

    transport = httpx.ASGITransport(app=_contact_app(session_factory, settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/webhooks/blip/messages/route-secret", json=tracking)
        unauthorized = await client.post("/webhooks/blip/messages/wrong-token", json=tracking)

    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is False
    assert unauthorized.status_code == 401
    async with session_factory() as session:
        assert (await session.scalars(select(InboundEvent))).all() == []
        assert (await session.scalars(select(OutboxJob))).all() == []


@pytest.mark.asyncio
async def test_blip_contact_update_is_reduced_to_the_allowed_fields_before_storing(
    session_factory,
    settings,
) -> None:
    contact = _contact_update(
        name="Ana Souza",
        email="Ana@Example.com",
        phoneNumber="+5511999999999",
        taxDocument="123.456.789-00",
        extras={
            "crmId": 42,
            "areaDeAtuacao": "Marketing",
            "RA": "2024001",
            "queue": "receptivo",
            "bsuid": "BR.123",
            "activecampaign:abc": "ana@example.com",
            "motivacao": "  ",
        },
    )

    transport = httpx.ASGITransport(app=_contact_app(session_factory, settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhooks/blip/messages/route-secret", json=contact)

    assert response.json()["accepted"] is True
    async with session_factory() as session:
        event = (await session.scalars(select(InboundEvent))).one()
        job = (await session.scalars(select(OutboxJob))).one()
    expected = {
        "identity": _CONTACT_CUSTOMER,
        "name": "Ana Souza",
        "email": "ana@example.com",
        "custom_attributes": {
            "crm_id": 42,
            "area_atuacao": "Marketing",
            "ra": "2024001",
            "cpf": "123.456.789-00",
        },
    }
    assert (event.event_type, event.payload) == ("contact", expected)
    assert (job.kind, job.payload["body"]) == ("blip_contact", expected)
    stored = str(event.payload) + str(job.payload)
    for dropped in ("+5511999999999", "receptivo", "BR.123", "activecampaign", "lastMessageDate"):
        assert dropped not in stored


@pytest.mark.asyncio
async def test_repeated_contact_updates_are_stored_only_when_the_profile_changes(
    session_factory,
    settings,
) -> None:
    transport = httpx.ASGITransport(app=_contact_app(session_factory, settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = [
            (await client.post("/webhooks/blip/messages/route-secret", json=payload)).json()
            for payload in (
                _contact_update(name="Ana"),
                _contact_update(name="Ana", lastMessageDate="2026-09-29T12:00:00.000Z"),
                _contact_update(name="Ana Souza"),
                _contact_update(name="Ana"),
            )
        ]

    assert [result["duplicate"] for result in results] == [False, True, False, False]
    async with session_factory() as session:
        names = [
            event.payload["name"]
            for event in (await session.scalars(select(InboundEvent).order_by(InboundEvent.id)))
        ]
    assert names == ["Ana", "Ana Souza", "Ana"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"extras": {"queue": "receptivo", "bsuid": "BR.1"}},
        {"name": "+55 11 99999-9999", "phoneNumber": "+5511999999999"},
        {},
    ],
)
async def test_contact_update_without_mirrorable_fields_is_ignored(
    session_factory,
    settings,
    fields,
) -> None:
    transport = httpx.ASGITransport(app=_contact_app(session_factory, settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/blip/messages/route-secret", json=_contact_update(**fields)
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is False
    async with session_factory() as session:
        assert (await session.scalars(select(InboundEvent))).all() == []


@pytest.mark.asyncio
async def test_contact_sync_can_be_switched_off(session_factory, settings) -> None:
    settings.blip_contact_sync = False

    transport = httpx.ASGITransport(app=_contact_app(session_factory, settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/blip/messages/route-secret", json=_contact_update(name="Ana")
        )

    assert response.json()["accepted"] is False
    async with session_factory() as session:
        assert (await session.scalars(select(InboundEvent))).all() == []


_CUSTOMER_MESSAGE = {
    "id": "message-1",
    "from": "551199999999@wa.gw.msging.net",
    "to": "mybot@msging.net",
    "type": "text/plain",
    "content": "Hello",
}


@pytest.mark.asyncio
async def test_unified_blip_webhook_dispatches_by_payload_shape(session_factory, settings) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)
    bot_message = {**_CUSTOMER_MESSAGE, "id": "message-2", "from": "mybot@msging.net/router-1"}
    notification = {"id": "message-1", "event": "received"}
    tracking = {"identity": _CUSTOMER_MESSAGE["from"], "category": "flow", "action": "Start"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.post("/webhooks/blip/route-secret", json=payload)
            for payload in (_CUSTOMER_MESSAGE, bot_message, notification, tracking)
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert responses[3].json()["accepted"] is False
    async with session_factory() as session:
        jobs = (await session.scalars(select(OutboxJob).order_by(OutboxJob.id))).all()
        assert [job.kind for job in jobs] == ["blip_message", "blip_message", "blip_notification"]


@pytest.mark.asyncio
async def test_unified_blip_webhook_accepts_root_path_with_header_token(
    session_factory,
    settings,
) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(settings=settings, session_factory=session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.post("/", json=_CUSTOMER_MESSAGE)
        wrong_path_token = await client.post("/webhooks/blip/wrong", json=_CUSTOMER_MESSAGE)
        accepted = await client.post(
            "/", json=_CUSTOMER_MESSAGE, headers={"X-Bridge-Token": "route-secret"}
        )

    assert (anonymous.status_code, wrong_path_token.status_code) == (401, 401)
    assert accepted.status_code == 200
    async with session_factory() as session:
        assert len((await session.scalars(select(InboundEvent))).all()) == 1
