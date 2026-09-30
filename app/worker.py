from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import init_db
from app.integrations.errors import IntegrationError
from app.models import InboundEvent, MessageDelivery, OutboxJob
from app.runtime import Runtime, create_runtime
from app.services.bridge import BridgeService
from app.services.queue import utcnow

logger = logging.getLogger(__name__)


async def run_once(runtime: Runtime) -> bool:
    async with runtime.session_factory() as session:
        now = utcnow()
        stale_before = now - timedelta(minutes=5)
        stale_jobs = list(
            (
                await session.scalars(
                    select(OutboxJob).where(
                        OutboxJob.status == "processing",
                        OutboxJob.updated_at < stale_before,
                    )
                )
            ).all()
        )
        for stale_job in stale_jobs:
            if stale_job.attempts >= runtime.settings.max_delivery_attempts:
                stale_job.status = "failed"
            else:
                stale_job.status = "pending"
                stale_job.next_attempt_at = now
        await session.commit()

        job = await session.scalar(
            select(OutboxJob)
            .where(
                OutboxJob.status == "pending",
                OutboxJob.next_attempt_at <= now,
            )
            .order_by(OutboxJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return False
        job.status = "processing"
        job.attempts += 1
        await session.commit()

        event = await session.get(InboundEvent, job.payload.get("event_id"))
        if not event:
            await _mark_job_failed(
                session,
                job,
                "Inbound event no longer exists",
                retryable=False,
                runtime=runtime,
            )
            return True

        service = BridgeService(
            session=session,
            settings=runtime.settings,
            blip=runtime.blip,
            chatwoot=runtime.chatwoot,
            media=runtime.media,
        )
        try:
            if job.kind == "blip_message":
                await service.process_blip_message(event)
            elif job.kind == "blip_notification":
                await service.process_blip_notification(event)
            elif job.kind == "blip_contact":
                await service.process_blip_contact(event)
            elif job.kind == "chatwoot_event":
                await service.process_chatwoot_event(event)
            else:
                raise IntegrationError(f"Unknown outbox job kind: {job.kind}", retryable=False)
        except IntegrationError as exc:
            await _mark_job_failed(
                session,
                job,
                str(exc),
                retryable=exc.retryable,
                max_attempts=runtime.settings.max_delivery_attempts,
                event=event,
                runtime=runtime,
            )
        except Exception as exc:
            logger.exception("Unhandled bridge worker error for job %s", job.id)
            await _mark_job_failed(
                session,
                job,
                str(exc),
                retryable=True,
                max_attempts=runtime.settings.max_delivery_attempts,
                event=event,
                runtime=runtime,
            )
        else:
            job.status = "succeeded"
            job.last_error = None
            await session.commit()
        return True


async def _mark_job_failed(
    session,
    job: OutboxJob,
    error: str,
    *,
    retryable: bool,
    max_attempts: int = 8,
    event: InboundEvent | None = None,
    runtime: Runtime | None = None,
) -> None:
    job_id = job.id
    event_id = event.id if event else None
    attempts = job.attempts
    await session.rollback()
    job = await session.get(OutboxJob, job_id)
    event = await session.get(InboundEvent, event_id) if event_id else None
    if not job:
        return

    terminal = not retryable or attempts >= max_attempts
    job.last_error = error[:2000]
    if event:
        event.last_error = error[:2000]
        event.status = "failed" if terminal else "retrying"
        event.attempts = attempts
    if not terminal:
        job.status = "pending"
        job.next_attempt_at = utcnow() + timedelta(seconds=min(300, 2**attempts))
    else:
        job.status = "failed"
    await session.commit()

    if (
        terminal
        and runtime
        and event
        and event.provider == "blip"
        and event.event_type == "message"
        and runtime.settings.blip_ack_messages
    ):
        message_id = event.payload.get("id")
        customer_identity = event.payload.get("from")
        bot_identity = runtime.settings.blip_bot_identity
        is_bot_message = bool(bot_identity) and str(customer_identity).split("/")[0] == bot_identity
        if message_id and customer_identity and not is_bot_message:
            try:
                await runtime.blip.send_notification(
                    message_id=str(message_id),
                    to=str(customer_identity),
                    event="failed",
                    reason={"code": 500, "description": error[:500]},
                )
            except Exception:
                logger.exception("Could not send terminal BLiP failure notification")

    if terminal and runtime and event and event.provider == "chatwoot":
        await _mark_outbound_delivery_failed(session, runtime, event, error)


async def _mark_outbound_delivery_failed(
    session,
    runtime: Runtime,
    event: InboundEvent,
    error: str,
) -> None:
    if event.event_type != "message_created":
        return
    message_id = BridgeService._optional_int(event.payload.get("id"))
    conversation_id = BridgeService._conversation_id(event.payload)
    if message_id is None or conversation_id is None:
        return
    delivery = await session.scalar(
        select(MessageDelivery).where(MessageDelivery.chatwoot_message_id == message_id)
    )
    if not delivery or delivery.status != "sending":
        return
    delivery.status = "failed"
    delivery.last_error = error[:2000]
    await session.commit()
    try:
        await runtime.chatwoot.update_message_status(
            conversation_id=conversation_id,
            message_id=message_id,
            status="failed",
            external_error=error[:500],
        )
    except Exception:
        logger.exception("Could not mark terminal Chatwoot delivery failure")


async def run_worker(runtime: Runtime) -> None:
    last_reconciliation = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            did_work = await run_once(runtime)
        except Exception:
            logger.exception("Worker iteration failed")
            did_work = False
        now = loop.time()
        if (
            runtime.settings.blip_ticket_tag_sync_enabled
            and now - last_reconciliation >= runtime.settings.blip_label_poll_seconds
        ):
            try:
                async with runtime.session_factory() as session:
                    service = BridgeService(
                        session=session,
                        settings=runtime.settings,
                        blip=runtime.blip,
                        chatwoot=runtime.chatwoot,
                    )
                    await service.reconcile_blip_tags()
            except Exception:
                logger.exception("Tag reconciliation iteration failed")
            last_reconciliation = now
        if not did_work:
            await asyncio.sleep(runtime.settings.worker_poll_seconds)


async def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    runtime = create_runtime(get_settings())
    if runtime.settings.auto_create_schema:
        await init_db(runtime.engine)
    try:
        await run_worker(runtime)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
