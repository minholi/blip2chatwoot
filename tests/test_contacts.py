from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.models import ConversationMapping, InboundEvent
from app.services.bridge import BridgeService
from app.services.contact_profile import extract_contact_profile

CUSTOMER = "551199999999@wa.gw.msging.net"
PROFILE = {
    "identity": CUSTOMER,
    "name": "Ana Souza",
    "email": "ana@example.com",
    "custom_attributes": {"crm_id": 42, "curso": "MBA"},
}


def _chatwoot(contact: dict) -> AsyncMock:
    chatwoot = AsyncMock(spec=ChatwootClient)
    chatwoot.get_contact.return_value = {"payload": {"id": 100, **contact}}
    chatwoot.update_contact.return_value = {}
    return chatwoot


def _mapping(settings) -> ConversationMapping:
    return ConversationMapping(
        blip_bot_identity=settings.blip_bot_identity,
        blip_customer_identity=CUSTOMER,
        chatwoot_account_id=settings.chatwoot_account_id,
        chatwoot_contact_id=100,
        chatwoot_source_id="source-1",
        chatwoot_conversation_id=200,
    )


def _event(profile: dict, *, key: int = 1, status: str = "received") -> InboundEvent:
    return InboundEvent(
        provider="blip",
        external_id=f"contact:{profile['identity']}:digest{key}:0",
        event_type="contact",
        payload=profile,
        status=status,
    )


async def _apply(
    session_factory, settings, chatwoot, profile=PROFILE, *, before=()
) -> InboundEvent:
    async with session_factory() as session:
        session.add(_mapping(settings))
        for previous in before:
            session.add(previous)
        event = _event(profile, key=len(before) + 1)
        session.add(event)
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )
        await service.process_blip_contact(event)
        return event


def test_profile_keeps_only_the_allowed_fields() -> None:
    profile = extract_contact_profile(
        {
            "identity": CUSTOMER,
            "name": "  Ana  ",
            "email": "ANA@Example.com",
            "phoneNumber": "+5511999999999",
            "taxDocument": "",
            "extras": {
                "areaAtuacao": "First",
                "areaDeAtuacao": "Second",
                "leadScore": 7.5,
                "hasTicket": True,
                "motivacao": {"nested": "no"},
                "unknown": "x",
            },
        }
    )

    assert profile == {
        "identity": CUSTOMER,
        "name": "Ana",
        "email": "ana@example.com",
        "custom_attributes": {"area_atuacao": "First", "lead_score": 7.5},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"identity": "no-at-sign", "name": "Ana"},
        {"identity": CUSTOMER, "name": "5511999999999", "email": "not-an-email"},
        {"identity": CUSTOMER, "extras": "not-a-dict"},
    ],
)
def test_profile_is_none_without_usable_fields(payload) -> None:
    assert extract_contact_profile(payload) is None


def test_long_text_is_truncated() -> None:
    profile = extract_contact_profile({"identity": CUSTOMER, "extras": {"motivacao": "x" * 5000}})

    assert profile is not None
    assert len(profile["custom_attributes"]["motivacao"]) == 1000


@pytest.mark.asyncio
async def test_profile_is_applied_over_a_placeholder_name_keeping_other_attributes(
    session_factory, settings
) -> None:
    chatwoot = _chatwoot(
        {"name": "+551199999999", "email": None, "custom_attributes": {"team_note": "keep"}}
    )

    event = await _apply(session_factory, settings, chatwoot)

    chatwoot.update_contact.assert_awaited_once_with(
        contact_id=100,
        name="Ana Souza",
        email="ana@example.com",
        custom_attributes={"team_note": "keep", "crm_id": 42, "curso": "MBA"},
    )
    assert event.status == "processed"


@pytest.mark.asyncio
async def test_name_typed_by_an_agent_is_not_overwritten(session_factory, settings) -> None:
    chatwoot = _chatwoot({"name": "Ana (VIP)", "email": None, "custom_attributes": {}})

    await _apply(session_factory, settings, chatwoot)

    kwargs = chatwoot.update_contact.await_args.kwargs
    assert "name" not in kwargs
    assert kwargs["email"] == "ana@example.com"


@pytest.mark.asyncio
async def test_name_previously_set_from_blip_follows_later_changes(
    session_factory, settings
) -> None:
    previous = _event({"identity": CUSTOMER, "name": "Ana"}, key=0, status="processed")
    chatwoot = _chatwoot({"name": "Ana", "email": "ana@example.com", "custom_attributes": {}})

    await _apply(session_factory, settings, chatwoot, before=[previous])

    assert chatwoot.update_contact.await_args.kwargs["name"] == "Ana Souza"


@pytest.mark.asyncio
async def test_nothing_is_sent_when_the_contact_is_already_up_to_date(
    session_factory, settings
) -> None:
    chatwoot = _chatwoot(
        {
            "name": "Ana Souza",
            "email": "Ana@Example.com",
            "custom_attributes": {"crm_id": 42, "curso": "MBA", "extra": "x"},
        }
    )

    event = await _apply(session_factory, settings, chatwoot)

    chatwoot.update_contact.assert_not_awaited()
    assert event.status == "processed"


@pytest.mark.asyncio
async def test_email_already_taken_still_updates_the_rest(session_factory, settings) -> None:
    chatwoot = _chatwoot({"name": "+551199999999", "email": None, "custom_attributes": {}})
    chatwoot.update_contact.side_effect = [
        IntegrationError("Email has already been taken", retryable=False, status_code=422),
        {},
    ]

    await _apply(session_factory, settings, chatwoot)

    first, second = chatwoot.update_contact.await_args_list
    assert first.kwargs["email"] == "ana@example.com"
    assert "email" not in second.kwargs
    assert second.kwargs["name"] == "Ana Souza"


@pytest.mark.asyncio
async def test_other_chatwoot_errors_are_left_to_the_worker(session_factory, settings) -> None:
    chatwoot = _chatwoot({"name": "+551199999999", "email": None, "custom_attributes": {}})
    chatwoot.update_contact.side_effect = IntegrationError("boom", retryable=True, status_code=500)

    with pytest.raises(IntegrationError):
        await _apply(session_factory, settings, chatwoot)


@pytest.mark.asyncio
async def test_profile_for_a_customer_without_conversation_is_deferred(
    session_factory, settings
) -> None:
    chatwoot = _chatwoot({})
    async with session_factory() as session:
        event = _event(PROFILE)
        session.add(event)
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )

        await service.process_blip_contact(event)

        assert event.status == "deferred"
        chatwoot.get_contact.assert_not_awaited()


def _first_message(settings) -> InboundEvent:
    return InboundEvent(
        provider="blip",
        external_id="message:first",
        event_type="message",
        payload={
            "id": "first",
            "from": CUSTOMER,
            "to": settings.blip_bot_identity,
            "type": "text/plain",
            "content": "Hi",
        },
    )


def _new_conversation_chatwoot(contact: dict) -> AsyncMock:
    chatwoot = _chatwoot(contact)
    chatwoot.create_contact.return_value = {
        "payload": {
            "contact": {"id": 100},
            "contact_inbox": {"source_id": "source-1", "inbox": {"id": 20}},
        }
    }
    chatwoot.create_conversation.return_value = {"id": 200}
    chatwoot.create_message.return_value = {"id": 300}
    chatwoot.get_messages.return_value = []
    return chatwoot


@pytest.mark.asyncio
async def test_deferred_profile_is_applied_when_the_conversation_is_created(
    session_factory, settings
) -> None:
    chatwoot = _new_conversation_chatwoot(
        {"name": "+551199999999", "email": None, "custom_attributes": {}}
    )
    async with session_factory() as session:
        stored = _event(PROFILE, status="deferred")
        message = _first_message(settings)
        session.add_all([stored, message])
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )

        await service.process_blip_message(message)

        chatwoot.update_contact.assert_awaited_once()
        assert chatwoot.update_contact.await_args.kwargs["name"] == "Ana Souza"
        assert stored.status == "processed"
        assert message.status == "processed"


@pytest.mark.asyncio
async def test_profile_failure_never_blocks_the_customer_message(session_factory, settings) -> None:
    chatwoot = _new_conversation_chatwoot(
        {"name": "+551199999999", "email": None, "custom_attributes": {}}
    )
    chatwoot.update_contact.side_effect = IntegrationError("down", retryable=True, status_code=503)
    async with session_factory() as session:
        stored = _event(PROFILE, status="deferred")
        message = _first_message(settings)
        session.add_all([stored, message])
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )

        await service.process_blip_message(message)

        chatwoot.create_message.assert_awaited_once()
        assert message.status == "processed"
        assert stored.status == "deferred"
        assert (await session.scalars(select(ConversationMapping))).one().chatwoot_contact_id == 100
