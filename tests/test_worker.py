from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.errors import IntegrationError
from app.models import InboundEvent, OutboxJob
from app.services.queue import utcnow
from app.worker import run_once


@pytest.mark.asyncio
async def test_terminal_blip_failure_is_acknowledged_as_failed(session_factory, settings) -> None:
    settings.max_delivery_attempts = 1
    blip = AsyncMock()
    chatwoot = AsyncMock()
    chatwoot.create_contact.side_effect = IntegrationError("Chatwoot unavailable")

    async with session_factory() as session:
        event = InboundEvent(
            provider="blip",
            external_id="message:terminal",
            event_type="message",
            payload={
                "id": "message-terminal",
                "from": "551199999999@wa.gw.msging.net",
                "to": settings.blip_bot_identity,
                "type": "text/plain",
                "content": "Hello",
            },
        )
        session.add(event)
        await session.flush()
        session.add(
            OutboxJob(
                kind="blip_message",
                idempotency_key="event:blip:message:terminal",
                payload={"event_id": event.id, "body": event.payload},
                next_attempt_at=utcnow(),
            )
        )
        await session.commit()

    runtime = SimpleNamespace(
        settings=settings,
        session_factory=session_factory,
        blip=blip,
        chatwoot=chatwoot,
    )
    assert await run_once(runtime)

    async with session_factory() as session:
        job = await session.scalar(select(OutboxJob))
        saved_event = await session.scalar(select(InboundEvent))
        assert job is not None and job.status == "failed"
        assert saved_event is not None and saved_event.status == "failed"

    blip.send_notification.assert_awaited_once_with(
        message_id="message-terminal",
        to="551199999999@wa.gw.msging.net",
        event="failed",
        reason={"code": 500, "description": "Chatwoot unavailable"},
    )
