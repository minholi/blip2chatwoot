from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InboundEvent
from app.services.queue import enqueue_event

# BLiP contact `extras` that are copied to Chatwoot custom attributes ({BLiP key: Chatwoot key}).
# Everything else (bot state, campaign ids, ActiveCampaign links, template variables, ...) is
# dropped before anything is stored: the contact payload carries student personal data.
_LEAD_EXTRAS = {
    "crmId": "crm_id",
    "areaAtuacao": "area_atuacao",
    "areaDeAtuacao": "area_atuacao",
    "formacao": "formacao",
    "anoConclusaoGraduacao": "ano_conclusao_graduacao",
    "motivacao": "motivacao",
    "leadScore": "lead_score",
    "prioridade": "prioridade",
    "origin": "origin",
    "recentOrigin": "recent_origin",
    "webVoucher": "web_voucher",
    "emailDoVendedor": "vendedor_email",
}
_STUDENT_EXTRAS = {
    "RA": "ra",
    "CURSO": "curso",
    "MODALIDADE": "modalidade",
    "SITUACAOALUNO": "situacao_aluno",
    "WINUSUARIO": "win_usuario",
    "SCORE": "score",
}
CONTACT_EXTRAS = {**_LEAD_EXTRAS, **_STUDENT_EXTRAS}
_MAX_TEXT = 1000
PHONE_LIKE = re.compile(r"^\+?[\d\s().-]{8,}$")


def is_contact_update(payload: dict[str, Any]) -> bool:
    """Contact updates carry `lastMessageDate`; tracking events carry `category`/`action`."""
    return (
        "identity" in payload
        and "from" not in payload
        and "lastMessageDate" in payload
        and "category" not in payload
        and "action" not in payload
    )


def extract_contact_profile(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a BLiP contact update to the fields worth mirroring, or None when there are none."""
    identity = payload.get("identity")
    if not isinstance(identity, str) or "@" not in identity:
        return None
    profile: dict[str, Any] = {"identity": identity}

    name = _scalar(payload.get("name"), limit=255)
    if isinstance(name, str) and not PHONE_LIKE.match(name):
        profile["name"] = name
    email = _scalar(payload.get("email"), limit=255)
    if isinstance(email, str) and "@" in email:
        profile["email"] = email.lower()

    attributes: dict[str, Any] = {}
    extras = payload.get("extras")
    if isinstance(extras, dict):
        for source, target in CONTACT_EXTRAS.items():
            value = _scalar(extras.get(source))
            if value is not None:
                attributes.setdefault(target, value)
    tax_document = _scalar(payload.get("taxDocument"), limit=64)
    if tax_document is not None:
        attributes["cpf"] = tax_document
    if attributes:
        profile["custom_attributes"] = attributes
    return profile if len(profile) > 1 else None


def _scalar(value: Any, *, limit: int = _MAX_TEXT) -> str | int | float | bool | None:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value.strip()[:limit] or None
    return None


async def latest_contact_event(
    session: AsyncSession,
    identity: str,
    *,
    before_id: int | None = None,
    status: str | None = None,
) -> InboundEvent | None:
    query = select(InboundEvent).where(
        InboundEvent.provider == "blip",
        InboundEvent.event_type == "contact",
        InboundEvent.external_id.startswith(f"contact:{identity}:", autoescape=True),
    )
    if before_id is not None:
        query = query.where(InboundEvent.id < before_id)
    if status is not None:
        query = query.where(InboundEvent.status == status)
    return await session.scalar(query.order_by(InboundEvent.id.desc()).limit(1))


async def enqueue_contact_profile(
    session: AsyncSession,
    profile: dict[str, Any],
) -> tuple[InboundEvent, bool]:
    """Queue a profile unless it equals the last one stored for the same customer.

    The previous event id is part of the key so that a value going A → B → A is not swallowed by the
    dedupe of the first A, while a webhook delivered twice still collapses into one event.
    """
    latest = await latest_contact_event(session, profile["identity"])
    if latest is not None and latest.payload == profile:
        return latest, False
    digest = hashlib.sha256(
        json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return await enqueue_event(
        session,
        provider="blip",
        external_id=f"contact:{profile['identity']}:{digest}:{latest.id if latest else 0}",
        delivery_id=None,
        event_type="contact",
        payload=profile,
        job_kind="blip_contact",
    )
