import pytest
from sqlalchemy import select

from app.models import InboundEvent, OutboxJob
from app.services.queue import enqueue_event


@pytest.mark.asyncio
async def test_enqueue_event_is_idempotent(session_factory) -> None:
    async with session_factory() as session:
        first, created = await enqueue_event(
            session,
            provider="blip",
            external_id="message:1",
            delivery_id=None,
            event_type="message",
            payload={"id": "1", "type": "text/plain", "from": "user@wa.gw.msging.net"},
            job_kind="blip_message",
        )
        second, duplicate_created = await enqueue_event(
            session,
            provider="blip",
            external_id="message:1",
            delivery_id=None,
            event_type="message",
            payload={"id": "1", "type": "text/plain", "from": "user@wa.gw.msging.net"},
            job_kind="blip_message",
        )

        assert created is True
        assert duplicate_created is False
        assert first.id == second.id
        assert len((await session.scalars(select(InboundEvent))).all()) == 1
        assert len((await session.scalars(select(OutboxJob))).all()) == 1
