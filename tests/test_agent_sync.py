import itertools
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.models import ConversationMapping, InboundEvent
from app.services import bridge
from app.services.bridge import BridgeService

CUSTOMER = "551199999999@wa.gw.msging.net"
JANE = "jane.doe%40example.com@blip.ai"
JOHN = "john%40example.com@blip.ai"


@pytest.fixture(autouse=True)
def _fresh_agent_caches():
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    bridge._AGENT_USER_IDS.clear()
    bridge._AGENT_LOCKS.clear()
    bridge._ATTENDANTS.names.clear()
    bridge._ATTENDANTS.loaded_at = None


def _chatwoot(agents: list[dict] | None = None) -> AsyncMock:
    chatwoot = AsyncMock(spec=ChatwootClient)
    chatwoot.create_contact.return_value = {
        "payload": {
            "contact": {"id": 100},
            "contact_inbox": {"source_id": "source-1", "inbox": {"id": 20}},
        }
    }
    chatwoot.create_conversation.return_value = {"id": 200}
    message_ids = itertools.count(300)
    chatwoot.create_message.side_effect = lambda **_: {"id": next(message_ids)}
    chatwoot.get_messages.return_value = []
    chatwoot.list_agents.return_value = agents or []
    chatwoot.create_agent.return_value = {"id": 501}
    return chatwoot


def _agent_event(message_id: str, agent: str | None = JANE, *, human: bool = True) -> InboundEvent:
    metadata: dict[str, str] = {}
    if human:
        metadata["#messageEmitter"] = "Human"
    if agent:
        metadata["#message.agentIdentity"] = agent
    return InboundEvent(
        provider="blip",
        external_id=f"message:{message_id}",
        event_type="message",
        payload={
            "id": message_id,
            "from": "mybot@msging.net/router-1",
            "to": CUSTOMER,
            "type": "text/plain",
            "content": "Hello",
            "metadata": metadata,
        },
    )


async def _process(session_factory, settings, chatwoot, *events: InboundEvent, blip=None):
    async with session_factory() as session:
        service = BridgeService(
            session=session,
            settings=settings,
            blip=blip or AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )
        for event in events:
            session.add(event)
            await session.commit()
            await service.process_blip_message(event)
        return await session.scalar(select(ConversationMapping))


@pytest.mark.asyncio
async def test_agent_found_by_email_joins_the_inbox_and_gets_the_conversation(
    session_factory,
    settings,
) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot([{"id": 7, "email": "Jane.Doe@Example.com"}])
    event = _agent_event("out-1")

    mapping = await _process(session_factory, settings, chatwoot, event)

    chatwoot.create_agent.assert_not_awaited()
    chatwoot.add_inbox_agents.assert_awaited_once_with([7])
    chatwoot.assign_conversation.assert_awaited_once_with(conversation_id=200, assignee_id=7)
    assert mapping.blip_agent_identity == "jane.doe@example.com"
    assert event.status == "processed"
    # The message itself is still mirrored by the Agent Bot, so echo protection is unchanged.
    assert chatwoot.create_message.await_args.kwargs["as_agent_bot"] is True


@pytest.mark.asyncio
async def test_unknown_agent_is_created_added_to_the_inbox_and_assigned(
    session_factory,
    settings,
) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot()

    mapping = await _process(session_factory, settings, chatwoot, _agent_event("out-1"))

    chatwoot.create_agent.assert_awaited_once_with(name="Jane Doe", email="jane.doe@example.com")
    chatwoot.add_inbox_agents.assert_awaited_once_with([501])
    chatwoot.assign_conversation.assert_awaited_once_with(conversation_id=200, assignee_id=501)
    assert mapping.blip_agent_identity == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_same_agent_is_assigned_and_looked_up_once(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot([{"id": 7, "email": "jane.doe@example.com"}])

    await _process(
        session_factory,
        settings,
        chatwoot,
        _agent_event("out-1"),
        _agent_event("out-2"),
    )

    chatwoot.assign_conversation.assert_awaited_once()
    chatwoot.list_agents.assert_awaited_once()
    chatwoot.add_inbox_agents.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_different_agent_takes_over_the_conversation(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot(
        [
            {"id": 7, "email": "jane.doe@example.com"},
            {"id": 8, "email": "john@example.com"},
        ]
    )

    mapping = await _process(
        session_factory,
        settings,
        chatwoot,
        _agent_event("out-1", JANE),
        _agent_event("out-2", JOHN),
    )

    assigned = [call.kwargs["assignee_id"] for call in chatwoot.assign_conversation.await_args_list]
    assert assigned == [7, 8]
    assert mapping.blip_agent_identity == "john@example.com"


@pytest.mark.asyncio
async def test_switch_off_makes_no_agent_calls(session_factory, settings) -> None:
    assert settings.chatwoot_agent_sync is False
    chatwoot = _chatwoot()

    mapping = await _process(session_factory, settings, chatwoot, _agent_event("out-1"))

    chatwoot.list_agents.assert_not_awaited()
    chatwoot.create_agent.assert_not_awaited()
    chatwoot.assign_conversation.assert_not_awaited()
    assert mapping.blip_agent_identity is None


@pytest.mark.asyncio
async def test_failed_assignment_does_not_fail_the_event_and_is_retried(
    session_factory,
    settings,
) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot([{"id": 7, "email": "jane.doe@example.com"}])
    chatwoot.assign_conversation.side_effect = [
        IntegrationError("Chatwoot returned HTTP 500", retryable=True, status_code=500),
        {},
    ]
    first, second = _agent_event("out-1"), _agent_event("out-2")

    async with session_factory() as session:
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )
        session.add(first)
        await session.commit()
        await service.process_blip_message(first)
        mapping = await session.scalar(select(ConversationMapping))
        assert first.status == "processed"
        assert mapping.blip_agent_identity is None

        session.add(second)
        await session.commit()
        await service.process_blip_message(second)
        assert mapping.blip_agent_identity == "jane.doe@example.com"

    assert chatwoot.assign_conversation.await_count == 2


@pytest.mark.asyncio
async def test_creation_conflict_falls_back_to_the_existing_agent(
    session_factory,
    settings,
) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot()
    chatwoot.list_agents.side_effect = [[], [{"id": 9, "email": "jane.doe@example.com"}]]
    chatwoot.create_agent.side_effect = IntegrationError(
        "Chatwoot returned HTTP 422", retryable=False, status_code=422
    )

    await _process(session_factory, settings, chatwoot, _agent_event("out-1"))

    chatwoot.add_inbox_agents.assert_awaited_once_with([9])
    chatwoot.assign_conversation.assert_awaited_once_with(conversation_id=200, assignee_id=9)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _agent_event("out-1", human=False),
        _agent_event("out-1", agent=None),
        _agent_event("out-1", agent="not-an-email"),
    ],
    ids=["bot", "human-without-identity", "identity-not-an-email"],
)
async def test_only_desk_agents_with_an_email_are_assigned(
    session_factory,
    settings,
    event,
) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot()

    mapping = await _process(session_factory, settings, chatwoot, event)

    chatwoot.list_agents.assert_not_awaited()
    chatwoot.assign_conversation.assert_not_awaited()
    assert mapping.blip_agent_identity is None
    assert event.status == "processed"


@pytest.mark.asyncio
async def test_customer_messages_never_touch_the_assignment(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot()
    event = InboundEvent(
        provider="blip",
        external_id="message:in-1",
        event_type="message",
        payload={
            "id": "in-1",
            "from": CUSTOMER,
            "to": "mybot@msging.net",
            "type": "text/plain",
            "content": "Hi",
            "metadata": {"#messageEmitter": "Human", "#message.agentIdentity": JANE},
        },
    )

    await _process(session_factory, settings, chatwoot, event)

    chatwoot.assign_conversation.assert_not_awaited()


@pytest.mark.parametrize(
    ("email", "name"),
    [
        ("maria.silva@example.com", "Maria Silva"),
        ("joao_pedro-souza@example.com", "Joao Pedro Souza"),
        ("ana+blip@example.com", "Ana Blip"),
        ("x@example.com", "X"),
    ],
)
def test_agent_name_comes_from_the_email(email, name) -> None:
    assert BridgeService._agent_name_from_email(email) == name


def _blip_with_attendants(attendants: list[dict] | Exception) -> AsyncMock:
    blip = AsyncMock(spec=BlipClient)
    if isinstance(attendants, Exception):
        blip.get_attendants.side_effect = attendants
    else:
        blip.get_attendants.return_value = attendants
    return blip


JANE_ATTENDANT = {
    "identity": JANE,
    "email": "jane.doe@example.com",
    "fullName": "Jane Quincy Doe",
}


@pytest.mark.asyncio
async def test_new_agent_is_created_with_the_name_from_blip_desk(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot()
    blip = _blip_with_attendants([{"identity": JOHN, "fullName": "John"}, JANE_ATTENDANT])

    await _process(session_factory, settings, chatwoot, _agent_event("out-1"), blip=blip)

    chatwoot.create_agent.assert_awaited_once_with(
        name="Jane Quincy Doe", email="jane.doe@example.com"
    )


@pytest.mark.asyncio
async def test_name_is_found_by_the_email_field_when_the_identity_differs(
    session_factory,
    settings,
) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot()
    blip = _blip_with_attendants(
        [
            {
                "identity": "other%40example.com@blip.ai",
                "email": "Jane.Doe@Example.com",
                "fullName": "Jane Q",
            }
        ]
    )

    await _process(session_factory, settings, chatwoot, _agent_event("out-1"), blip=blip)

    assert chatwoot.create_agent.await_args.kwargs["name"] == "Jane Q"


@pytest.mark.asyncio
async def test_name_lookup_off_never_calls_blip(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    chatwoot = _chatwoot()
    blip = _blip_with_attendants([JANE_ATTENDANT])

    await _process(session_factory, settings, chatwoot, _agent_event("out-1"), blip=blip)

    blip.get_attendants.assert_not_awaited()
    assert chatwoot.create_agent.await_args.kwargs["name"] == "Jane Doe"


@pytest.mark.asyncio
async def test_existing_chatwoot_agent_needs_no_blip_lookup(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot([{"id": 7, "email": "jane.doe@example.com"}])
    blip = _blip_with_attendants([JANE_ATTENDANT])

    await _process(session_factory, settings, chatwoot, _agent_event("out-1"), blip=blip)

    blip.get_attendants.assert_not_awaited()
    chatwoot.create_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attendants",
    [
        [],
        [{"identity": JOHN, "fullName": "John"}],
        [{"identity": JANE, "email": "jane.doe@example.com", "fullName": "  "}],
        [{"fullName": "No Identity"}],
        IntegrationError("BLiP returned HTTP 401", retryable=False, status_code=401),
    ],
    ids=["empty", "other-people", "blank-name", "no-identity", "blip-refuses"],
)
async def test_name_falls_back_to_the_email_when_blip_has_no_answer(
    session_factory,
    settings,
    attendants,
) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot()

    mapping = await _process(
        session_factory,
        settings,
        chatwoot,
        _agent_event("out-1"),
        blip=_blip_with_attendants(attendants),
    )

    assert chatwoot.create_agent.await_args.kwargs["name"] == "Jane Doe"
    assert mapping.blip_agent_identity == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_transient_blip_error_defers_creating_the_agent(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot()
    blip = _blip_with_attendants([])
    blip.get_attendants.side_effect = [
        IntegrationError("BLiP returned HTTP 503", retryable=True, status_code=503),
        [JANE_ATTENDANT],
    ]
    first, second = _agent_event("out-1"), _agent_event("out-2")

    async with session_factory() as session:
        service = BridgeService(session=session, settings=settings, blip=blip, chatwoot=chatwoot)
        session.add(first)
        await session.commit()
        await service.process_blip_message(first)
        mapping = await session.scalar(select(ConversationMapping))
        assert first.status == "processed"
        assert mapping.blip_agent_identity is None
        chatwoot.create_agent.assert_not_awaited()

        session.add(second)
        await session.commit()
        await service.process_blip_message(second)

    chatwoot.create_agent.assert_awaited_once_with(
        name="Jane Quincy Doe", email="jane.doe@example.com"
    )


@pytest.mark.asyncio
async def test_attendants_are_read_once_for_several_new_agents(session_factory, settings) -> None:
    settings.chatwoot_agent_sync = True
    settings.blip_agent_name_lookup = True
    chatwoot = _chatwoot()
    chatwoot.create_agent.side_effect = [{"id": 501}, {"id": 502}]
    blip = _blip_with_attendants(
        [JANE_ATTENDANT, {"identity": JOHN, "email": "john@example.com", "fullName": "John Smith"}]
    )

    await _process(
        session_factory,
        settings,
        chatwoot,
        _agent_event("out-1", JANE),
        _agent_event("out-2", JOHN),
        blip=blip,
    )

    blip.get_attendants.assert_awaited_once()
    names = [call.kwargs["name"] for call in chatwoot.create_agent.await_args_list]
    assert names == ["Jane Quincy Doe", "John Smith"]
