from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InboundEvent, OutboxJob


def utcnow() -> datetime:
    return datetime.now(UTC)


async def enqueue_event(
    session: AsyncSession,
    *,
    provider: str,
    external_id: str,
    delivery_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    job_kind: str,
) -> tuple[InboundEvent, bool]:
    """Persist a webhook event and its outbox job exactly once."""
    existing = await session.scalar(
        select(InboundEvent).where(
            InboundEvent.provider == provider,
            InboundEvent.external_id == external_id,
        )
    )
    if existing:
        return existing, False

    event = InboundEvent(
        provider=provider,
        external_id=external_id,
        delivery_id=delivery_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    try:
        await session.flush()
        session.add(
            OutboxJob(
                kind=job_kind,
                idempotency_key=f"event:{provider}:{external_id}",
                payload={"event_id": event.id, "body": payload},
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(InboundEvent).where(
                InboundEvent.provider == provider,
                InboundEvent.external_id == external_id,
            )
        )
        if not existing:
            raise
        return existing, False
    return event, True
