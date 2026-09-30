from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.runtime import Runtime
from app.security import verify_chatwoot_signature
from app.services.contact_profile import (
    enqueue_contact_profile,
    extract_contact_profile,
    is_contact_update,
)
from app.services.queue import enqueue_event

router = APIRouter()


async def get_session(request: Request):
    runtime: Runtime = request.app.state.runtime
    async with runtime.session_factory() as session:
        yield session


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _check_blip_token(
    request: Request,
    supplied_token: str | None,
    *,
    expected: str,
) -> None:
    if not expected:
        return
    supplied = supplied_token or request.headers.get("X-Bridge-Token")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )


async def _json_body(request: Request) -> tuple[bytes, dict[str, Any]]:
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return raw_body, payload


def _is_blip_resource_update(payload: dict[str, Any]) -> bool:
    # BLiP tracking events and contact updates are keyed by `identity`, never by `from`.
    return "identity" in payload and "from" not in payload


async def _receive_blip_message(
    request: Request,
    session: AsyncSession,
    supplied_token: str | None = None,
) -> JSONResponse:
    _check_blip_token(
        request,
        supplied_token,
        expected=_runtime(request).settings.blip_inbound_path_token,
    )
    _, payload = await _json_body(request)
    return await _enqueue_blip_message(request, session, payload)


async def _enqueue_blip_message(
    request: Request,
    session: AsyncSession,
    payload: dict[str, Any],
) -> JSONResponse:
    if _is_blip_resource_update(payload):
        profile = (
            extract_contact_profile(payload)
            if is_contact_update(payload) and _runtime(request).settings.blip_contact_sync
            else None
        )
        if profile is None:
            return JSONResponse(
                status_code=200,
                content={"accepted": False, "ignored": "unsupported_resource"},
            )
        event, created = await enqueue_contact_profile(session, profile)
        return JSONResponse(
            status_code=200,
            content={"accepted": True, "duplicate": not created, "event_id": event.id},
        )
    message_id = payload.get("id")
    if not message_id or not payload.get("type") or "from" not in payload:
        raise HTTPException(status_code=422, detail="Invalid BLiP message envelope")

    runtime = _runtime(request)
    event, created = await enqueue_event(
        session,
        provider="blip",
        external_id=f"message:{runtime.settings.blip_bot_identity}:{message_id}",
        delivery_id=None,
        event_type="message",
        payload=payload,
        job_kind="blip_message",
    )
    return JSONResponse(
        status_code=200,
        content={"accepted": True, "duplicate": not created, "event_id": event.id},
    )


@router.post("/webhooks/blip/messages")
async def receive_blip_message(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    return await _receive_blip_message(request, session)


@router.post("/webhooks/blip/messages/{path_token}")
async def receive_blip_message_with_token(
    path_token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    return await _receive_blip_message(request, session, path_token)


@router.post("/webhooks/blip/notifications")
async def receive_blip_notification(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    _check_blip_token(
        request,
        None,
        expected=_runtime(request).settings.blip_notification_path_token,
    )
    raw_body, payload = await _json_body(request)
    return await _enqueue_blip_notification(session, raw_body, payload)


@router.post("/webhooks/blip/notifications/{path_token}")
async def receive_blip_notification_with_token(
    path_token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    _check_blip_token(
        request,
        path_token,
        expected=_runtime(request).settings.blip_notification_path_token,
    )
    raw_body, payload = await _json_body(request)
    return await _enqueue_blip_notification(session, raw_body, payload)


async def _receive_blip_webhook(
    request: Request,
    session: AsyncSession,
    supplied_token: str | None = None,
) -> JSONResponse:
    _check_blip_token(
        request,
        supplied_token,
        expected=_runtime(request).settings.blip_inbound_path_token,
    )
    raw_body, payload = await _json_body(request)
    is_notification = "event" in payload and "content" not in payload
    if is_notification and not _is_blip_resource_update(payload):
        return await _enqueue_blip_notification(session, raw_body, payload)
    return await _enqueue_blip_message(request, session, payload)


@router.post("/")
@router.post("/webhooks/blip")
async def receive_blip_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    return await _receive_blip_webhook(request, session)


@router.post("/webhooks/blip/{path_token}")
async def receive_blip_webhook_with_token(
    path_token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    return await _receive_blip_webhook(request, session, path_token)


async def _enqueue_blip_notification(
    session: AsyncSession,
    raw_body: bytes,
    payload: dict[str, Any],
) -> JSONResponse:
    message_id = payload.get("id")
    event_name = payload.get("event")
    if not message_id or not event_name:
        raise HTTPException(status_code=422, detail="Invalid BLiP notification envelope")
    digest = hashlib.sha256(raw_body).hexdigest()[:16]
    event, created = await enqueue_event(
        session,
        provider="blip",
        external_id=f"notification:{message_id}:{event_name}:{digest}",
        delivery_id=None,
        event_type="notification",
        payload=payload,
        job_kind="blip_notification",
    )
    return JSONResponse(
        status_code=200,
        content={"accepted": True, "duplicate": not created, "event_id": event.id},
    )


@router.post("/webhooks/chatwoot")
async def receive_chatwoot_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    runtime = _runtime(request)
    raw_body, payload = await _json_body(request)
    secret = runtime.settings.chatwoot_webhook_secret
    if secret and not verify_chatwoot_signature(
        secret=secret,
        timestamp=request.headers.get("X-Chatwoot-Timestamp"),
        signature=request.headers.get("X-Chatwoot-Signature"),
        raw_body=raw_body,
    ):
        raise HTTPException(status_code=401, detail="Invalid Chatwoot signature")

    event_name = payload.get("event")
    if not event_name:
        raise HTTPException(status_code=422, detail="Chatwoot event is missing")
    payload_id = payload.get("id") or (payload.get("conversation") or {}).get("id")
    if payload_id is None:
        raise HTTPException(status_code=422, detail="Chatwoot event id is missing")
    digest = hashlib.sha256(raw_body).hexdigest()[:16]
    account_id = (payload.get("account") or {}).get("id", runtime.settings.chatwoot_account_id)
    if str(account_id) != str(runtime.settings.chatwoot_account_id):
        raise HTTPException(status_code=403, detail="Chatwoot account mismatch")
    delivery_id = request.headers.get("X-Chatwoot-Delivery")
    external_id = (
        f"delivery:{delivery_id}"
        if delivery_id
        else f"{event_name}:{account_id}:{payload_id}:{digest}"
    )
    event, created = await enqueue_event(
        session,
        provider="chatwoot",
        external_id=external_id,
        delivery_id=delivery_id,
        event_type=str(event_name),
        payload=payload,
        job_kind="chatwoot_event",
    )
    return JSONResponse(
        status_code=200,
        content={"accepted": True, "duplicate": not created, "event_id": event.id},
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    runtime = _runtime(request)
    async with runtime.session_factory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready"}
