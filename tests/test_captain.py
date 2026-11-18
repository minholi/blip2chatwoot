"""Captain sessions started by a BLiP icebreaker (CAPTAIN_* settings)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.services.bridge import BridgeService
from app.services.queue import enqueue_event
from app.services.whatsapp_format import markdown_to_whatsapp
from app.worker import run_once

CUSTOMER = "551199999999@wa.gw.msging.net"
SUBBOT = "puccampinascamila@msging.net"


@pytest.fixture
def captain_settings(settings: Settings) -> Settings:
    settings.captain_trigger_phrases = "Resgatar meu cupom."
    settings.captain_session_labels = "camila, bot-venda-inicio"
    settings.captain_blip_subbot = SUBBOT
    settings.captain_replies_to_blip = True
    return settings


def _chatwoot(*, conversation_status: str = "pending", labels: list[str] | None = None):
    chatwoot = AsyncMock(spec=ChatwootClient)
    chatwoot.create_contact.return_value = {
        "payload": {
            "contact": {"id": 100},
            "contact_inbox": {"source_id": "source-1", "inbox": {"id": 20}},
        }
    }
    chatwoot.create_conversation.return_value = {"id": 200, "status": conversation_status}
    chatwoot.create_message.return_value = {"id": 300}
    chatwoot.get_messages.return_value = []
    chatwoot.get_conversation_labels.return_value = list(labels or [])
    return chatwoot


def _customer_event(content: str = "Resgatar meu cupom.", message_id: str = "m-1") -> InboundEvent:
    return InboundEvent(
        provider="blip",
        external_id=f"message:{message_id}",
        event_type="message",
        payload={
            "id": message_id,
            "from": CUSTOMER,
            "to": "mybot@msging.net",
            "type": "text/plain",
            "content": content,
        },
    )


def _mapping(settings: Settings, **overrides) -> ConversationMapping:
    values = {
        "blip_bot_identity": settings.blip_bot_identity,
        "blip_customer_identity": CUSTOMER,
        "chatwoot_account_id": settings.chatwoot_account_id,
        "chatwoot_contact_id": 100,
        "chatwoot_source_id": "source-1",
        "chatwoot_conversation_id": 200,
    }
    values.update(overrides)
    return ConversationMapping(**values)


async def _process(session, settings, blip, chatwoot, event) -> None:
    session.add(event)
    await session.commit()
    service = BridgeService(session=session, settings=settings, blip=blip, chatwoot=chatwoot)
    if event.provider == "blip":
        await service.process_blip_message(event)
    else:
        await service.process_chatwoot_event(event)


# --- settings ------------------------------------------------------------------------------


def test_captain_is_off_by_default(settings) -> None:
    assert not settings.captain_enabled
    assert not settings.blip_writes_enabled


def test_captain_phrases_and_labels_are_parsed(captain_settings) -> None:
    captain_settings.captain_trigger_phrases = " Resgatar  meu CUPOM. | Quero meu desconto "
    assert captain_settings.captain_trigger_phrase_set == {
        "resgatar meu cupom.",
        "quero meu desconto",
    }
    assert captain_settings.captain_session_label_list == ["camila", "bot-venda-inicio"]
    assert captain_settings.blip_writes_enabled


def test_captain_phrases_require_the_subbot() -> None:
    with pytest.raises(ValidationError, match="CAPTAIN_BLIP_SUBBOT"):
        Settings(_env_file=None, captain_trigger_phrases="Resgatar meu cupom.")


def test_follow_master_state_enables_the_captain_and_needs_the_subbot() -> None:
    with pytest.raises(ValidationError, match="CAPTAIN_BLIP_SUBBOT"):
        Settings(_env_file=None, captain_follow_master_state=True)
    settings = Settings(
        _env_file=None, captain_follow_master_state=True, captain_blip_subbot=SUBBOT
    )
    assert settings.captain_enabled
    assert settings.blip_writes_enabled
    assert not settings.captain_trigger_phrase_set


# --- icebreaker ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_icebreaker_starts_the_session_before_the_message_is_posted(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        event = _customer_event("  resgatar   MEU cupom. ")
        await _process(session, captain_settings, blip, chatwoot, event)

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is True
        assert mapping.status == "pending"
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio"]
        # Created pending (Captain on the inbox) and left pending: never opened, never toggled.
        chatwoot.toggle_status.assert_not_awaited()
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("add_conversation_labels") < names.index("create_message")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["camila", "bot-venda-inicio"]
        )
        assert event.status == "processed"
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_icebreaker_in_an_open_conversation_moves_it_to_pending(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(labels=["vip", "camila"])
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event())

        mapping = await session.scalar(select(ConversationMapping))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["vip", "camila", "bot-venda-inicio"]
        )
        chatwoot.ensure_labels.assert_awaited_once_with(["bot-venda-inicio"])
        assert mapping.captain_active is True
        assert mapping.status == "pending"


@pytest.mark.asyncio
async def test_new_conversation_created_open_goes_pending_on_icebreaker(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot(conversation_status="open")
    async with session_factory() as session:
        await _process(
            session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")


@pytest.mark.asyncio
async def test_retry_after_the_message_was_posted_does_not_restart_the_session(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        event = _customer_event()
        event.result_id = "300"
        await _process(session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, event)

        chatwoot.add_conversation_labels.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False


@pytest.mark.asyncio
async def test_retry_after_a_half_started_session_does_not_repeat_the_steps(
    session_factory, captain_settings
) -> None:
    """The labels were added but the status change failed: the retry finds them done."""
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(conversation_status="open")
    chatwoot.get_conversation_labels.side_effect = [[], ["camila", "bot-venda-inicio"]]
    chatwoot.toggle_status.side_effect = IntegrationError("down", retryable=True, status_code=503)
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        await session.commit()
        event = _customer_event()
        with pytest.raises(IntegrationError):
            await _process(session, captain_settings, blip, chatwoot, event)

        chatwoot.toggle_status.side_effect = None
        await _process(session, captain_settings, blip, chatwoot, event)

        chatwoot.add_conversation_labels.assert_awaited_once()
        chatwoot.toggle_status.assert_awaited()
        chatwoot.create_message.assert_awaited_once()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is True
        assert mapping.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["Resgatar meu cupom agora", "oi"])
async def test_other_text_is_not_an_icebreaker(session_factory, captain_settings, content) -> None:
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(
            session,
            captain_settings,
            AsyncMock(spec=BlipClient),
            chatwoot,
            _customer_event(content),
        )
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")
        chatwoot.add_conversation_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_icebreaker_is_ignored_when_the_feature_is_off(session_factory, settings) -> None:
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event())
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        assert mapping.status == "open"
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")


@pytest.mark.asyncio
async def test_bot_message_with_the_phrase_is_not_an_icebreaker(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        event = _customer_event()
        event.payload = {
            **event.payload,
            "from": "mybot@msging.net/instance",
            "to": CUSTOMER,
        }
        await _process(session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, event)
        chatwoot.add_conversation_labels.assert_not_awaited()


# --- customer returns ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("master_state", "status", "active"),
    [
        (SUBBOT, "pending", True),
        ("PucCampinasCamila@msging.net/instance-1", "pending", True),
        (None, "open", False),
        ("puccampinascomercial@msging.net", "open", False),
    ],
)
async def test_returning_customer_follows_the_router_state(
    session_factory, captain_settings, master_state, status, active
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = master_state
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))

        mapping = await session.scalar(select(ConversationMapping))
        blip.get_master_state.assert_awaited_once_with(CUSTOMER)
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status=status)
        assert mapping.status == status
        assert mapping.captain_active is active
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("toggle_status") < names.index("create_message")


@pytest.mark.asyncio
async def test_pending_session_with_the_customer_still_there_is_left_alone(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("quero IA"))
        chatwoot.toggle_status.assert_not_awaited()
        # Never mid-session: the labels of the session in progress are left alone.
        chatwoot.add_conversation_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_router_state_keeps_the_captain(session_factory, captain_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.side_effect = IntegrationError("down", retryable=True, status_code=503)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")


@pytest.mark.asyncio
async def test_open_session_conversation_is_not_touched(session_factory, captain_settings) -> None:
    """After a handoff the conversation is open: the customer's messages change nothing."""
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()
        chatwoot.toggle_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_without_session_reopens_as_before(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved"))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")


# --- labels left by an earlier session -----------------------------------------------------


@pytest.fixture
def reset_settings(captain_settings: Settings) -> Settings:
    captain_settings.captain_session_reset_label_prefixes = " saida-, bot-venda- "
    return captain_settings


def test_reset_prefixes_are_parsed(reset_settings) -> None:
    assert reset_settings.captain_session_reset_prefix_list == ["saida-", "bot-venda-"]
    assert Settings(_env_file=None).captain_session_reset_prefix_list == []


@pytest.mark.asyncio
async def test_new_session_drops_the_exit_and_funnel_labels_of_the_previous_one(
    session_factory, reset_settings
) -> None:
    """A stale `saida-sair` would otherwise fire the exit automation on the next resolve."""
    chatwoot = _chatwoot(
        labels=["vip", "saida-sair", "bot-venda-sem-interesse", "camila", "bot-venda-link-enviado"]
    )
    async with session_factory() as session:
        session.add(_mapping(reset_settings, status="resolved"))
        await session.commit()
        await _process(
            session, reset_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )

        chatwoot.ensure_labels.assert_awaited_once_with(["bot-venda-inicio"])
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["vip", "camila", "bot-venda-inicio"]
        )
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_labels == ["vip", "camila", "bot-venda-inicio"]
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_finished_session_resumed_for_the_captain_drops_the_stale_labels(
    session_factory, reset_settings
) -> None:
    """A night-window re-entry keeps the conversation but must not keep the exit label.

    The exit automation resolved the conversation and left `saida-sair`; `captain_active` was
    never cleared (the bridge only clears it when a message finds the customer out of the sub-bot),
    so the returning customer goes through `_reopen_for_customer`, not a session start.
    """
    reset_settings.captain_follow_master_state = True
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot(labels=["vip", "saida-sair", "bot-venda-link-enviado", "camila"])
    async with session_factory() as session:
        session.add(_mapping(reset_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, reset_settings, blip, chatwoot, _customer_event("já paguei"))

        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["vip", "camila", "bot-venda-inicio"]
        )
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.status == "pending"
        assert mapping.captain_active is True
        assert mapping.chatwoot_labels == ["vip", "camila", "bot-venda-inicio"]


@pytest.mark.asyncio
async def test_session_labels_matching_a_reset_prefix_are_kept(
    session_factory, reset_settings
) -> None:
    chatwoot = _chatwoot(labels=["camila", "bot-venda-inicio"])
    async with session_factory() as session:
        session.add(_mapping(reset_settings, status="open"))
        await session.commit()
        await _process(
            session, reset_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.add_conversation_labels.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio"]


@pytest.mark.asyncio
async def test_without_reset_prefixes_old_labels_stay(session_factory, captain_settings) -> None:
    chatwoot = _chatwoot(labels=["saida-sair"])
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved"))
        await session.commit()
        await _process(
            session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["saida-sair", "camila", "bot-venda-inicio"]
        )


@pytest.mark.asyncio
async def test_reset_works_with_only_prefixes_configured(session_factory, reset_settings) -> None:
    reset_settings.captain_session_labels = ""
    chatwoot = _chatwoot(labels=["saida-aluno", "vip"])
    async with session_factory() as session:
        session.add(_mapping(reset_settings, status="open"))
        await session.commit()
        await _process(
            session, reset_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.ensure_labels.assert_not_awaited()
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["vip"]
        )


@pytest.mark.asyncio
async def test_retry_of_the_same_icebreaker_does_not_touch_labels_again(
    session_factory, reset_settings
) -> None:
    chatwoot = _chatwoot(labels=["camila", "bot-venda-inicio"])
    async with session_factory() as session:
        session.add(_mapping(reset_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(
            session, reset_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.add_conversation_labels.assert_not_awaited()
        chatwoot.ensure_labels.assert_not_awaited()


# --- a new conversation per session ------------------------------------------------------------

HISTORY = [{"id": 1, "message_type": 0, "content": "Resgatar meu cupom."}]


@pytest.fixture
def per_session_settings(captain_settings: Settings) -> Settings:
    captain_settings.captain_new_conversation_per_session = True
    return captain_settings


def _chatwoot_with_history(*, new_status: str = "pending"):
    chatwoot = _chatwoot()
    chatwoot.get_messages.return_value = list(HISTORY)
    chatwoot.create_conversation.return_value = {"id": 201, "status": new_status}
    return chatwoot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "captain_active"),
    [
        ("resolved", False),  # the session ended and nobody wrote since
        ("resolved", True),  # left by an n8n exit: the flag is only cleared on the next message
        ("open", False),  # the customer wrote to the main bot and the conversation reopened
        ("open", True),  # handed to an agent
    ],
)
async def test_new_session_gets_a_new_conversation(
    session_factory, per_session_settings, status, captain_active
) -> None:
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(
            _mapping(
                per_session_settings,
                status=status,
                captain_active=captain_active,
                chatwoot_labels=["saida-sair", "bot-venda-link-enviado"],
                blip_agent_identity="ana@blip.ai",
            )
        )
        await session.commit()
        await _process(
            session, per_session_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )

        chatwoot.create_conversation.assert_awaited_once_with(source_id="source-1", contact_id=100)
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 201
        assert mapping.status == "pending"
        assert mapping.captain_active is True
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio"]
        assert mapping.blip_agent_identity is None
        # Nothing is removed from the old conversation; everything goes to the new one.
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=201, labels=["camila", "bot-venda-inicio"]
        )
        assert chatwoot.create_message.await_args.kwargs["conversation_id"] == 201
        chatwoot.toggle_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_icebreaker_repeated_mid_session_stays_in_the_same_conversation(
    session_factory, per_session_settings
) -> None:
    per_session_settings.captain_session_reset_label_prefixes = "saida-,bot-venda-"
    chatwoot = _chatwoot_with_history()
    chatwoot.get_conversation_labels.return_value = ["camila", "bot-venda-pre-inscricao"]
    async with session_factory() as session:
        session.add(_mapping(per_session_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(
            session, per_session_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )

        chatwoot.create_conversation.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 200
        # The funnel labels of the session in progress are not reset.
        assert "bot-venda-pre-inscricao" in mapping.chatwoot_labels


@pytest.mark.asyncio
async def test_conversation_without_real_messages_is_reused(
    session_factory, per_session_settings
) -> None:
    """A retry between creating the conversation and starting the session, or a fresh one."""
    chatwoot = _chatwoot_with_history()
    chatwoot.get_messages.return_value = [{"id": 1, "message_type": 2, "content": "status"}]
    async with session_factory() as session:
        session.add(_mapping(per_session_settings, status="pending"))
        await session.commit()
        await _process(
            session, per_session_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )

        chatwoot.create_conversation.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 200
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_first_contact_creates_one_conversation_only(
    session_factory, per_session_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _process(
            session, per_session_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.create_conversation.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_the_message_was_posted_keeps_the_new_conversation(
    session_factory, per_session_settings
) -> None:
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(per_session_settings, status="resolved", chatwoot_conversation_id=201))
        event = _customer_event()
        event.result_id = "300"
        await _process(session, per_session_settings, AsyncMock(spec=BlipClient), chatwoot, event)
        chatwoot.create_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_the_switch_the_conversation_is_reused(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved"))
        await session.commit()
        await _process(
            session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.create_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_night_window_entry_gets_a_new_conversation(
    session_factory, per_session_settings
) -> None:
    per_session_settings.captain_follow_master_state = True
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(per_session_settings, status="resolved"))
        await session.commit()
        await _process(session, per_session_settings, blip, chatwoot, _customer_event("boa noite"))

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 201
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_returning_customer_of_a_finished_session_keeps_the_conversation(
    session_factory, per_session_settings
) -> None:
    """Only an entry (icebreaker, night window) starts a new conversation; "oi" after a link
    stays in the session's conversation, which the Captain still has the context of."""
    per_session_settings.captain_follow_master_state = True
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(per_session_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, per_session_settings, blip, chatwoot, _customer_event("já paguei"))

        chatwoot.create_conversation.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 200
        assert mapping.status == "pending"


@pytest.mark.asyncio
async def test_events_of_a_replaced_conversation_are_ignored(
    session_factory, per_session_settings
) -> None:
    chatwoot = _chatwoot_with_history()
    blip = AsyncMock(spec=BlipClient)
    async with session_factory() as session:
        session.add(
            _mapping(
                per_session_settings,
                status="pending",
                captain_active=True,
                chatwoot_conversation_id=201,
            )
        )
        await session.commit()
        event = _chatwoot_message(conversation={"id": 200})
        await _process(session, per_session_settings, blip, chatwoot, event)

        assert event.status == "ignored"
        blip.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("switch", "ignored"), [(True, True), (False, False)])
async def test_delivery_notice_for_a_replaced_conversation_is_not_an_error(
    session_factory, per_session_settings, switch, ignored
) -> None:
    per_session_settings.captain_new_conversation_per_session = switch
    chatwoot = _chatwoot()
    chatwoot.update_message_status.side_effect = IntegrationError(
        "not found", status_code=404, retryable=False
    )
    async with session_factory() as session:
        mapping = _mapping(per_session_settings, chatwoot_conversation_id=201)
        session.add(mapping)
        await session.commit()
        session.add(
            MessageDelivery(
                mapping_id=mapping.id, chatwoot_message_id=300, blip_message_id="b-1", status="sent"
            )
        )
        event = InboundEvent(
            provider="blip",
            external_id="notification:b-1:consumed",
            event_type="notification",
            payload={"id": "b-1", "from": CUSTOMER, "event": "consumed"},
        )
        session.add(event)
        await session.commit()
        service = BridgeService(
            session=session,
            settings=per_session_settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )
        if ignored:
            await service.process_blip_notification(event)
            assert event.status == "processed"
        else:
            with pytest.raises(IntegrationError):
                await service.process_blip_notification(event)


# --- BLiP decides (night window): CAPTAIN_FOLLOW_MASTER_STATE ----------------------------------


@pytest.fixture
def follow_settings(captain_settings: Settings) -> Settings:
    captain_settings.captain_follow_master_state = True
    return captain_settings


@pytest.mark.asyncio
async def test_any_text_starts_the_session_while_the_router_has_the_customer_in_the_subbot(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot(conversation_status="open")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("boa noite"))

        mapping = await session.scalar(select(ConversationMapping))
        blip.get_master_state.assert_awaited_once_with(CUSTOMER)
        assert mapping.captain_active is True
        assert mapping.status == "pending"
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["camila", "bot-venda-inicio"]
        )
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("toggle_status") < names.index("create_message")


@pytest.mark.asyncio
async def test_new_conversation_of_a_customer_in_the_subbot_is_not_opened(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("boa noite"))
        # created pending by Chatwoot and kept pending: the only status change is none
        chatwoot.toggle_status.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.status == "pending"
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_resolved_conversation_of_a_customer_back_in_the_subbot_goes_pending(
    session_factory, follow_settings
) -> None:
    """Next night: the Captain resolved the last conversation and BLiP sends the customer again."""
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="resolved"))
        await session.commit()
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))

        mapping = await session.scalar(select(ConversationMapping))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        assert mapping.captain_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "puccampinascomercial@msging.net"])
async def test_customer_outside_the_subbot_is_handled_as_before(
    session_factory, follow_settings, state
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = state
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        assert mapping.status == "open"
        chatwoot.add_conversation_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_state_starts_no_session_and_still_mirrors(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.side_effect = IntegrationError("down", retryable=True, status_code=503)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        chatwoot.create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_state_read_when_a_session_is_active_or_the_message_was_already_posted(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="open", captain_active=True))
        await session.commit()
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi", "m-1"))
        retry = _customer_event("oi", "m-2")
        retry.result_id = "300"
        await _process(session, follow_settings, blip, chatwoot, retry)
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_messages_never_trigger_the_state_read(session_factory, follow_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="open"))
        event = _customer_event("Olá!")
        event.payload = {**event.payload, "from": "mybot@msging.net/instance", "to": CUSTOMER}
        await _process(session, follow_settings, blip, chatwoot, event)
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_is_not_read_when_the_switch_is_off(session_factory, captain_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()


# --- Captain replies -----------------------------------------------------------------------


def _chatwoot_message(*, sender: str = "captain_assistant", **overrides) -> InboundEvent:
    payload = {
        "event": "message_created",
        "id": 400,
        "content": "Plano 1: **12x de R$ 635,17**",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": sender},
        "conversation": {"id": 200},
    }
    payload.update(overrides)
    return InboundEvent(
        provider="chatwoot",
        external_id="message_created:400",
        event_type="message_created",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_captain_reply_in_a_session_is_sent_to_blip_as_whatsapp_text(
    session_factory, captain_settings
) -> None:
    captain_settings.chatwoot_replies_allowed_conversations = "999"  # agents' pilot list
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _chatwoot_message())

        blip.send_message.assert_awaited_once()
        sent = blip.send_message.await_args.kwargs
        assert sent["to"] == CUSTOMER
        assert sent["message_type"] == "text/plain"
        assert sent["content"] == "Plano 1: *12x de R$ 635,17*"
        delivery = await session.scalar(select(MessageDelivery))
        assert delivery.status == "sent"
        assert chatwoot.update_message_status.await_args == call(
            conversation_id=200, message_id=400, status="sent"
        )


@pytest.mark.asyncio
async def test_captain_media_caption_is_also_converted_to_whatsapp(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    message = _chatwoot_message(
        content="Veja o **material**",
        attachments=[
            {
                "file_type": "image",
                "content_type": "image/png",
                "data_url": "https://chatwoot.example/file.png",
            }
        ],
    )
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, message)

        sent = blip.send_message.await_args.kwargs
        assert sent["message_type"] == "application/vnd.lime.media-link+json"
        assert sent["content"]["text"] == "Veja o *material*"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "switch", "overrides"),
    [
        (False, True, {}),
        (True, False, {}),
        (True, True, {"private": True}),
        (True, True, {"content_attributes": {"blip_message_id": "echo"}}),
    ],
)
async def test_captain_reply_is_not_forwarded_outside_a_session(
    session_factory, captain_settings, active, switch, overrides
) -> None:
    captain_settings.captain_replies_to_blip = switch
    blip = AsyncMock(spec=BlipClient)
    async with session_factory() as session:
        session.add(_mapping(captain_settings, captain_active=active))
        await session.commit()
        await _process(session, captain_settings, blip, _chatwoot(), _chatwoot_message(**overrides))
        blip.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_captain_switch_does_not_forward_human_agents(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    async with session_factory() as session:
        session.add(_mapping(captain_settings, captain_active=True))
        await session.commit()
        await _process(
            session, captain_settings, blip, _chatwoot(), _chatwoot_message(sender="user")
        )
        blip.send_message.assert_not_awaited()


# --- BLiP master-state ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (200, {"status": "success", "resource": SUBBOT}, SUBBOT),
        (200, {"status": "success", "resource": None}, None),
        (200, {"status": "failure", "reason": {"code": 67, "description": "not found"}}, None),
        (404, {"status": "failure"}, None),
    ],
)
async def test_master_state_lookup(settings, status_code, body, expected) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blip = BlipClient(settings, client)
    assert await blip.get_master_state(CUSTOMER) == expected
    await client.aclose()

    sent = requests[0]
    assert sent.url.path.endswith("/commands")
    assert sent.headers["Authorization"] == "Key blip-key"
    # On the hot path of every customer message: capped below the shared HTTP timeout.
    assert sent.extensions["timeout"]["read"] == 5.0
    payload = json.loads(sent.content)
    assert payload["to"] == "postmaster@msging.net"
    assert payload["method"] == "get"
    assert payload["uri"] == f"/contexts/{CUSTOMER}/master-state"


@pytest.mark.asyncio
async def test_master_state_other_failure_raises(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failure", "reason": {"code": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(IntegrationError):
        await BlipClient(settings, client).get_master_state(CUSTOMER)
    await client.aclose()


# --- formatting ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**IA e Negócios** em **16/11/2026**", "*IA e Negócios* em *16/11/2026*"),
        ("__sublinhado__ e ~~riscado~~", "*sublinhado* e ~riscado~"),
        ("## Planos\n* Plano 1\n* Plano 2", "*Planos*\n- Plano 1\n- Plano 2"),
        ("[Pague aqui](https://x.example/p?a=1)", "Pague aqui: https://x.example/p?a=1"),
        ("[https://x.example](https://x.example)", "https://x.example"),
        ("2**3 e *já negrito*", "2**3 e *já negrito*"),
        ("sem formatação", "sem formatação"),
    ],
)
def test_markdown_to_whatsapp(text, expected) -> None:
    assert markdown_to_whatsapp(text) == expected


# --- start endpoint: BLiP tells the bridge a customer entered the sub-bot -------------------

START_TOKEN = "start-secret"


@pytest.fixture
def start_settings(captain_settings: Settings) -> Settings:
    captain_settings.captain_trigger_phrases = ""
    captain_settings.captain_start_token = START_TOKEN
    captain_settings.captain_new_conversation_per_session = True
    return captain_settings


def _start_event(
    *, origin: str | None = "leadads", message_id: str | None = "m-9", content: str | None = "Oi"
) -> InboundEvent:
    payload = {"identity": CUSTOMER}
    if origin:
        payload["origin"] = origin
    if message_id:
        payload["message_id"] = message_id
    if content:
        payload["content"] = content
    return InboundEvent(
        provider="captain",
        external_id=f"captain_start:{CUSTOMER}:{message_id or 't1'}",
        event_type="captain_start",
        payload=payload,
    )


async def _start(session, settings, chatwoot, event, blip=None) -> None:
    session.add(event)
    await session.commit()
    service = BridgeService(
        session=session,
        settings=settings,
        blip=blip or AsyncMock(spec=BlipClient),
        chatwoot=chatwoot,
    )
    await service.process_captain_start(event)


def test_start_token_enables_the_captain_and_needs_the_subbot() -> None:
    with pytest.raises(ValidationError, match="CAPTAIN_START_TOKEN"):
        Settings(_env_file=None, captain_start_token="x")
    settings = Settings(_env_file=None, captain_start_token="x", captain_blip_subbot=SUBBOT)
    assert settings.captain_enabled


@pytest.mark.asyncio
async def test_start_for_a_new_lead_opens_the_session_and_posts_the_message(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _start(session, start_settings, chatwoot, _start_event())

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is True
        assert mapping.status == "pending"
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio", "origem-leadads"]
        chatwoot.create_conversation.assert_awaited_once()
        chatwoot.toggle_status.assert_not_awaited()
        kwargs = chatwoot.create_message.await_args.kwargs
        assert kwargs["content"] == "Oi"
        assert kwargs["message_type"] == "incoming"
        assert kwargs["content_attributes"] == {"captain_start": True, "blip_message_id": "m-9"}
        event = await session.scalar(select(InboundEvent))
        assert event.status == "processed"
        assert event.result_id == "300"
        delivery = await session.scalar(select(MessageDelivery))
        assert (delivery.blip_message_id, delivery.status) == ("m-9", "mirrored")


@pytest.mark.asyncio
async def test_start_creates_the_shadow_ticket_when_tag_sync_is_on(
    session_factory, start_settings
) -> None:
    start_settings.blip_ticket_tag_sync_enabled = True
    blip = AsyncMock(spec=BlipClient)
    blip.create_shadow_ticket.return_value = "ticket-1"
    async with session_factory() as session:
        await _start(session, start_settings, _chatwoot(), _start_event(), blip=blip)

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.blip_ticket_id == "ticket-1"
        blip.create_shadow_ticket.assert_awaited_once_with(CUSTOMER)


@pytest.mark.asyncio
async def test_start_for_a_finished_session_gets_a_new_conversation(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="resolved", chatwoot_labels=["saida-sair"]))
        await session.commit()
        await _start(session, start_settings, chatwoot, _start_event(origin="noturno"))

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 201
        assert mapping.captain_active is True
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio", "origem-noturno"]
        assert chatwoot.create_message.await_args.kwargs["conversation_id"] == 201


@pytest.mark.asyncio
async def test_start_while_the_session_is_ongoing_does_nothing(
    session_factory, start_settings
) -> None:
    """The icebreaker (or the router state) already opened it."""
    chatwoot = _chatwoot_with_history()
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="pending", captain_active=True))
        await session.commit()
        await _start(session, start_settings, chatwoot, _start_event())

        chatwoot.create_conversation.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()
        event = await session.scalar(select(InboundEvent))
        assert event.status == "ignored"


@pytest.mark.asyncio
async def test_start_reuses_the_conversation_whose_only_message_is_the_copy_of_the_lead(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    chatwoot.get_messages.return_value = [
        {"id": 1, "message_type": 0, "content_attributes": {"blip_message_id": "m-9"}}
    ]
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="open"))
        await session.commit()
        await _start(session, start_settings, chatwoot, _start_event())

        chatwoot.create_conversation.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 200
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_start_after_the_copy_was_mirrored_posts_the_marker(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    chatwoot.get_messages.return_value = [
        {"id": 77, "message_type": 0, "content_attributes": {"blip_message_id": "m-9"}}
    ]
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="open"))
        await session.commit()
        await _start(session, start_settings, chatwoot, _start_event())

        kwargs = chatwoot.create_message.await_args.kwargs
        assert kwargs["content"] == start_settings.captain_start_fallback_text
        assert kwargs["content_attributes"] == {"captain_start": True}
        assert (await session.scalar(select(MessageDelivery))) is None


@pytest.mark.asyncio
async def test_copy_arriving_before_the_start_is_not_posted_twice(
    session_factory, start_settings
) -> None:
    """BLiP's normal webhook mirrors the lead's message first; the start call must not re-post it.

    An inbound mirror records no MessageDelivery, so the copy has to be found in the conversation.
    """
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _process(
            session,
            start_settings,
            AsyncMock(spec=BlipClient),
            chatwoot,
            _customer_event("Oi", message_id="m-9"),
        )
        chatwoot.get_messages.return_value = [
            {"id": 77, "message_type": 0, "content_attributes": {"blip_message_id": "m-9"}}
        ]
        chatwoot.create_message.reset_mock()
        await _start(
            session, start_settings, chatwoot, _start_event(content="Oi", message_id="m-9")
        )

        kwargs = chatwoot.create_message.await_args.kwargs
        assert kwargs["content"] == start_settings.captain_start_fallback_text
        assert "blip_message_id" not in kwargs["content_attributes"]
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_conversation_id == 200
        assert mapping.captain_active is True
        assert (await session.scalar(select(MessageDelivery))) is None


@pytest.mark.asyncio
async def test_later_copy_of_the_start_message_is_dropped_as_an_echo(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _start(session, start_settings, chatwoot, _start_event())
        chatwoot.create_message.reset_mock()
        await _process(
            session,
            start_settings,
            AsyncMock(spec=BlipClient),
            chatwoot,
            _customer_event("Oi", message_id="m-9"),
        )
        chatwoot.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_without_content_or_origin_posts_the_marker_without_origin_label(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _start(
            session,
            start_settings,
            chatwoot,
            _start_event(origin=None, message_id=None, content=None),
        )
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio"]
        kwargs = chatwoot.create_message.await_args.kwargs
        assert kwargs["content"] == start_settings.captain_start_fallback_text
        assert (await session.scalar(select(MessageDelivery))) is None


@pytest.mark.asyncio
async def test_start_origin_is_slugged(session_factory, start_settings) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await _start(session, start_settings, chatwoot, _start_event(origin=" Lead Ads! "))
        mapping = await session.scalar(select(ConversationMapping))
        assert "origem-lead-ads" in mapping.chatwoot_labels


@pytest.mark.asyncio
async def test_start_retry_after_the_session_opened_does_not_restart_it(
    session_factory, start_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="pending", captain_active=True))
        await session.commit()
        event = _start_event()
        event.result_id = "session:200"
        await _start(session, start_settings, chatwoot, event)

        chatwoot.create_conversation.assert_not_awaited()
        chatwoot.add_conversation_labels.assert_not_awaited()
        assert chatwoot.create_message.await_args.kwargs["content"] == "Oi"
        assert event.result_id == "300"


@pytest.mark.asyncio
async def test_start_retry_after_it_finished_is_ignored(session_factory, start_settings) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(start_settings, status="pending", captain_active=True))
        await session.commit()
        event = _start_event()
        event.result_id = "300"
        await _start(session, start_settings, chatwoot, event)
        chatwoot.create_message.assert_not_awaited()
        assert event.status == "ignored"


@pytest.mark.asyncio
async def test_start_for_a_bot_node_is_ignored(session_factory, start_settings) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        event = _start_event()
        event.payload = {**event.payload, "identity": "otherbot@msging.net/pod"}
        await _start(session, start_settings, chatwoot, event)
        chatwoot.create_contact.assert_not_awaited()
        assert event.status == "ignored"


@pytest.mark.asyncio
async def test_worker_dispatches_captain_start(session_factory, start_settings) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        await enqueue_event(
            session,
            provider="captain",
            external_id="captain_start:x:1",
            delivery_id=None,
            event_type="captain_start",
            payload={"identity": CUSTOMER, "content": "Oi"},
            job_kind="captain_start",
        )
    runtime = SimpleNamespace(
        settings=start_settings,
        session_factory=session_factory,
        blip=AsyncMock(spec=BlipClient),
        chatwoot=chatwoot,
        media=None,
    )
    assert await run_once(runtime) is True
    chatwoot.create_message.assert_awaited_once()
