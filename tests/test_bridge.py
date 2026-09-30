from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.integrations.media import DownloadedMedia, MediaDownloader
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.schemas import BlipMessage
from app.services.bridge import BridgeService, is_bot_node


@pytest.mark.asyncio
async def test_blip_message_creates_chatwoot_mapping_and_acknowledges(
    session_factory,
    settings,
) -> None:
    settings.blip_ack_messages = True
    blip = AsyncMock(spec=BlipClient)
    chatwoot = AsyncMock(spec=ChatwootClient)
    chatwoot.create_contact.return_value = {
        "payload": {
            "contact": {"id": 100},
            "contact_inbox": {"source_id": "source-1", "inbox": {"id": 20}},
        }
    }
    chatwoot.create_conversation.return_value = {"id": 200}
    chatwoot.create_message.return_value = {"id": 300}
    chatwoot.get_messages.return_value = []

    async with session_factory() as session:
        event = InboundEvent(
            provider="blip",
            external_id="message:1",
            event_type="message",
            payload={
                "id": "blip-message-1",
                "from": "551199999999@wa.gw.msging.net",
                "to": "mybot@msging.net",
                "type": "text/plain",
                "content": "Need help",
            },
        )
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=blip,
            chatwoot=chatwoot,
        )
        await service.process_blip_message(event)

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping is not None
        assert mapping.blip_customer_identity == "551199999999@wa.gw.msging.net"
        chatwoot.create_message.assert_awaited_once_with(
            conversation_id=200,
            content="Need help",
            message_type="incoming",
            content_attributes={"blip_message_id": "blip-message-1"},
        )
        blip.send_notification.assert_awaited_once_with(
            message_id="blip-message-1",
            to="551199999999@wa.gw.msging.net",
            event="consumed",
        )
        assert event.status == "processed"
        assert event.result_id == "300"


@pytest.mark.asyncio
async def test_chatwoot_agent_reply_is_sent_to_blip(session_factory, settings) -> None:
    settings.chatwoot_replies_to_blip = True
    blip = AsyncMock(spec=BlipClient)
    chatwoot = AsyncMock(spec=ChatwootClient)
    async with session_factory() as session:
        mapping = ConversationMapping(
            blip_bot_identity=settings.blip_bot_identity,
            blip_customer_identity="551199999999@wa.gw.msging.net",
            chatwoot_account_id=settings.chatwoot_account_id,
            chatwoot_contact_id=100,
            chatwoot_source_id="source-1",
            chatwoot_conversation_id=200,
        )
        session.add(mapping)
        await session.commit()

        event = InboundEvent(
            provider="chatwoot",
            external_id="message_created:200",
            event_type="message_created",
            payload={
                "event": "message_created",
                "id": 300,
                "content": "A human will help you.",
                "message_type": "outgoing",
                "private": False,
                "sender": {"type": "user"},
                "conversation": {"id": 200},
            },
        )
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=blip,
            chatwoot=chatwoot,
        )
        await service.process_chatwoot_event(event)

        blip.send_message.assert_awaited_once()
        call = blip.send_message.await_args.kwargs
        assert call["to"] == "551199999999@wa.gw.msging.net"
        assert call["content"] == "A human will help you."
        chatwoot.update_message_status.assert_awaited_once_with(
            conversation_id=200,
            message_id=300,
            status="sent",
        )
        delivery = await session.scalar(select(MessageDelivery))
        assert delivery is not None
        assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_blip_desk_ticket_envelope_is_not_forwarded(session_factory, settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = AsyncMock(spec=ChatwootClient)
    async with session_factory() as session:
        event = InboundEvent(
            provider="blip",
            external_id="message:ticket",
            event_type="message",
            payload={
                "id": "blip-ticket-1",
                "from": "551199999999@wa.gw.msging.net",
                "to": "mybot@msging.net",
                "type": "application/vnd.iris.ticket+json",
                "content": {"id": "ticket-1", "status": "Waiting", "team": "Default"},
            },
        )
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=blip,
            chatwoot=chatwoot,
        )
        await service.process_blip_message(event)

        assert event.status == "ignored"
        chatwoot.create_contact.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()
        blip.send_notification.assert_not_awaited()


def _blip_reply(replied: dict, in_reply_to: dict) -> BlipMessage:
    return BlipMessage.model_validate(
        {
            "id": "blip-reply-1",
            "from": "551199999999@wa.gw.msging.net",
            "to": "mybot@msging.net",
            "type": "application/vnd.lime.reply+json",
            "content": {"replied": replied, "inReplyTo": in_reply_to},
        }
    )


def test_blip_reply_is_rendered_with_the_quoted_message() -> None:
    message = _blip_reply(
        {"type": "text/plain", "value": "Morning works"},
        {
            "id": "sent-1",
            "type": "text/plain",
            "value": "Morning or afternoon?\nPick one",
            "direction": "sent",
        },
    )

    assert BridgeService._blip_content_as_text(message) == (
        "> Morning or afternoon?\n> Pick one\n\nMorning works"
    )


def test_blip_reply_to_non_text_message_has_no_quote() -> None:
    message = _blip_reply(
        {"type": "text/plain", "value": "Morning works"},
        {"id": "sent-1", "type": "application/json", "value": {"menu": []}, "direction": "sent"},
    )

    assert BridgeService._blip_content_as_text(message) == "Morning works"


def test_blip_reply_without_text_falls_back_to_serialized_content() -> None:
    message = _blip_reply(
        {"type": "image/png", "value": {"uri": "https://files.example/a.png"}},
        {"id": "sent-1", "type": "text/plain", "value": "Send a photo", "direction": "sent"},
    )

    text = BridgeService._blip_content_as_text(message)

    assert text.startswith("[BLiP message type: application/vnd.lime.reply+json]\n")


CUSTOMER = "551199999999@wa.gw.msging.net"


def _chatwoot_mock() -> AsyncMock:
    chatwoot = AsyncMock(spec=ChatwootClient)
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


def _blip_event(external_id: str, **fields) -> InboundEvent:
    payload = {"id": external_id, "to": "mybot@msging.net", "type": "text/plain", **fields}
    return InboundEvent(
        provider="blip",
        external_id=f"message:{external_id}",
        event_type="message",
        payload=payload,
    )


def _mapping(settings, *, status: str = "open") -> ConversationMapping:
    return ConversationMapping(
        blip_bot_identity=settings.blip_bot_identity,
        blip_customer_identity=CUSTOMER,
        chatwoot_account_id=settings.chatwoot_account_id,
        chatwoot_contact_id=100,
        chatwoot_source_id="source-1",
        chatwoot_conversation_id=200,
        status=status,
    )


@pytest.mark.asyncio
async def test_bot_message_is_mirrored_as_outgoing_and_never_acknowledged(
    session_factory,
    settings,
) -> None:
    settings.blip_ack_messages = True
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _blip_event(
            "blip-out-1",
            **{
                "from": f"{settings.blip_bot_identity}/router-1",
                "to": CUSTOMER,
                "content": "Hi there",
                "metadata": {
                    "#messageEmitter": "Human",
                    "#message.agentIdentity": "agent@example.com",
                },
            },
        )
        session.add(event)
        await session.commit()

        service = BridgeService(session=session, settings=settings, blip=blip, chatwoot=chatwoot)
        await service.process_blip_message(event)

        chatwoot.create_contact.assert_awaited_once()
        assert chatwoot.create_contact.await_args.kwargs["identifier"] == CUSTOMER
        chatwoot.create_message.assert_awaited_once_with(
            conversation_id=200,
            content="[BLiP agent: agent@example.com]\nHi there",
            message_type="outgoing",
            content_attributes={"blip_message_id": "blip-out-1", "blip_direction": "outbound"},
            as_agent_bot=True,
        )
        blip.send_notification.assert_not_awaited()
        assert event.status == "processed"
        delivery = await session.scalar(select(MessageDelivery))
        assert delivery is not None
        assert (delivery.status, delivery.chatwoot_message_id) == ("mirrored", 300)
        assert delivery.blip_message_id == "blip-out-1"


def test_bot_message_without_human_metadata_is_labelled_as_bot() -> None:
    message = BlipMessage.model_validate(
        {
            "id": "m1",
            "from": "mybot@msging.net/router-1",
            "to": CUSTOMER,
            "type": "text/plain",
            "content": "Hello",
            "metadata": {"#messageKind": "Response"},
        }
    )

    assert BridgeService._outbound_label(message) == "BLiP bot"


@pytest.mark.asyncio
async def test_customer_message_is_not_acknowledged_unless_enabled(
    session_factory,
    settings,
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _blip_event("blip-in-1", **{"from": CUSTOMER, "content": "Need help"})
        session.add(event)
        await session.commit()

        service = BridgeService(session=session, settings=settings, blip=blip, chatwoot=chatwoot)
        await service.process_blip_message(event)

        assert event.status == "processed"
        assert chatwoot.create_message.await_args.kwargs["message_type"] == "incoming"
        blip.send_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_type", "content"),
    [
        ("application/json", {"messaging_product": "whatsapp", "typing_indicator": {}}),
        ("application/vnd.lime.chatstate+json", {"state": "composing"}),
    ],
)
async def test_typing_signals_from_the_bot_are_not_mirrored(
    session_factory,
    settings,
    message_type,
    content,
) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _blip_event(
            "blip-typing-1",
            **{"from": f"{settings.blip_bot_identity}/router-1", "to": CUSTOMER},
        )
        event.payload = {**event.payload, "type": message_type, "content": content}
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session, settings=settings, blip=AsyncMock(spec=BlipClient), chatwoot=chatwoot
        )
        await service.process_blip_message(event)

        assert event.status == "ignored"
        chatwoot.create_contact.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", ["mybot@msging.net", "postmaster@msging.net", ""])
async def test_bot_messages_without_a_customer_recipient_are_ignored(
    session_factory,
    settings,
    recipient,
) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _blip_event(
            "blip-odd-1",
            **{"from": f"{settings.blip_bot_identity}/router-1", "to": recipient, "content": "x"},
        )
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session, settings=settings, blip=AsyncMock(spec=BlipClient), chatwoot=chatwoot
        )
        await service.process_blip_message(event)

        assert event.status == "ignored"
        chatwoot.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_message_does_not_reopen_a_resolved_conversation(
    session_factory,
    settings,
) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        session.add(_mapping(settings, status="resolved"))
        outbound = _blip_event(
            "blip-survey-1",
            **{
                "from": f"{settings.blip_bot_identity}/router-1",
                "to": CUSTOMER,
                "content": "Rate us",
            },
        )
        session.add(outbound)
        await session.commit()

        service = BridgeService(
            session=session, settings=settings, blip=AsyncMock(spec=BlipClient), chatwoot=chatwoot
        )
        await service.process_blip_message(outbound)
        chatwoot.toggle_status.assert_not_awaited()
        chatwoot.create_contact.assert_not_awaited()

        inbound = _blip_event("blip-in-2", **{"from": CUSTOMER, "content": "Thanks"})
        session.add(inbound)
        await session.commit()
        await service.process_blip_message(inbound)
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")


@pytest.mark.asyncio
async def test_message_sent_by_the_bridge_and_echoed_by_blip_is_not_mirrored_again(
    session_factory,
    settings,
) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        mapping = _mapping(settings)
        session.add(mapping)
        await session.commit()
        session.add(
            MessageDelivery(
                mapping_id=mapping.id,
                chatwoot_message_id=300,
                blip_message_id="bridge-sent-1",
                status="sent",
            )
        )
        echo = _blip_event(
            "bridge-sent-1",
            **{"from": f"{settings.blip_bot_identity}/router-1", "to": CUSTOMER, "content": "Hi"},
        )
        session.add(echo)
        await session.commit()

        service = BridgeService(
            session=session, settings=settings, blip=AsyncMock(spec=BlipClient), chatwoot=chatwoot
        )
        await service.process_blip_message(echo)

        assert echo.status == "ignored"
        chatwoot.create_message.assert_not_awaited()


def _agent_reply(message_id: int = 300, **overrides) -> dict:
    return {
        "event": "message_created",
        "id": message_id,
        "content": "A human will help you.",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 200},
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["replies_disabled", "mirrored_echo", "marker_in_payload"],
)
async def test_chatwoot_message_is_not_forwarded_to_blip(session_factory, settings, case) -> None:
    settings.chatwoot_replies_to_blip = case != "replies_disabled"
    blip = AsyncMock(spec=BlipClient)
    payload = _agent_reply()
    async with session_factory() as session:
        mapping = _mapping(settings)
        session.add(mapping)
        await session.commit()
        if case == "mirrored_echo":
            session.add(
                MessageDelivery(
                    mapping_id=mapping.id,
                    chatwoot_message_id=300,
                    blip_message_id="blip-out-1",
                    status="mirrored",
                )
            )
        if case == "marker_in_payload":
            payload["content_attributes"] = {"blip_message_id": "blip-out-1"}
        event = InboundEvent(
            provider="chatwoot",
            external_id=f"message_created:{case}",
            event_type="message_created",
            payload=payload,
        )
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session, settings=settings, blip=blip, chatwoot=AsyncMock(spec=ChatwootClient)
        )
        await service.process_chatwoot_event(event)

        blip.send_message.assert_not_awaited()
        assert event.status == "processed"


def test_media_link_is_rendered_with_type_and_caption() -> None:
    message = BlipMessage.model_validate(
        {
            "id": "m1",
            "from": "mybot@msging.net/router-1",
            "to": CUSTOMER,
            "type": "application/vnd.lime.media-link+json",
            "content": {
                "type": "image/png",
                "uri": "https://files.example/a.png",
                "title": "Price list",
                "text": "Price list",
            },
        }
    )

    assert BridgeService._blip_content_as_text(message) == (
        "[BLiP attachment (image/png): https://files.example/a.png]\nPrice list"
    )


def _blip_content(message_type: str, content: object, **metadata: object) -> BlipMessage:
    return BlipMessage.model_validate(
        {
            "id": "m1",
            "from": "mybot@msging.net",
            "to": CUSTOMER,
            "type": message_type,
            "content": content,
            "metadata": metadata,
        }
    )


def test_signed_media_url_is_shown_without_its_query() -> None:
    signed = "https://files.example/thread-medias/a.pdf?sv=2020&se=2026-01-01T00%3A30Z&sig=SECRET"
    message = _blip_content(
        "application/vnd.lime.media-link+json",
        {"type": "application/pdf", "uri": signed, "title": "a.pdf", "text": "a.pdf"},
    )

    text = BridgeService._blip_content_as_text(message)

    assert (
        text
        == "[BLiP attachment (application/pdf): https://files.example/thread-medias/a.pdf]\na.pdf"
    )
    assert "SECRET" not in text


def test_unsigned_media_url_keeps_its_query() -> None:
    message = _blip_content(
        "application/vnd.lime.media-link+json",
        {"type": "image/png", "uri": "https://files.example/a.png?id=7"},
    )

    assert "https://files.example/a.png?id=7" in BridgeService._blip_content_as_text(message)


def test_template_is_rendered_with_filled_placeholders_and_buttons() -> None:
    message = _blip_content(
        "application/json",
        {
            "type": "template",
            "template": {
                "name": "welcome_v1",
                "components": [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}],
            },
            "templateContent": {
                "name": "welcome_v1",
                "category": "MARKETING",
                "components": [
                    {"type": "BODY", "text": "Hello {{1}}!\\n\\nEnrollment is open. {{2}}"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "QUICK_REPLY", "text": "Tell me more"}],
                    },
                ],
            },
        },
    )

    assert BridgeService._blip_content_as_text(message) == (
        "[BLiP template: welcome_v1]\n\n"
        "Hello Ana!\n\nEnrollment is open. {{2}}\n\n"
        "Options: [Tell me more]"
    )


def test_template_keeps_unresolved_blip_variables_as_received() -> None:
    message = _blip_content(
        "application/json",
        {
            "type": "template",
            "template": {
                "name": "welcome_v1",
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": "${contact.extras.1}"}],
                    }
                ],
            },
            "templateContent": {"components": [{"type": "BODY", "text": "Hi {{1}}"}]},
        },
    )

    assert (
        BridgeService._blip_content_as_text(message) == "[BLiP template]\n\nHi ${contact.extras.1}"
    )


def test_template_without_text_falls_back_to_serialized_content() -> None:
    message = _blip_content(
        "application/json", {"type": "template", "templateContent": {"components": []}}
    )

    assert BridgeService._blip_content_as_text(message).startswith(
        "[BLiP message type: application/json]\n"
    )


def test_interactive_buttons_are_rendered_as_body_and_options() -> None:
    message = _blip_content(
        "application/json",
        {
            "recipient_type": "individual",
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Are you a student?"},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "1", "title": "Yes"}},
                        {"type": "reply", "reply": {"id": "2", "title": "Not yet"}},
                    ]
                },
            },
        },
    )

    assert BridgeService._blip_content_as_text(message) == (
        "Are you a student?\n\nOptions: [Yes] [Not yet]"
    )


def test_interactive_list_rows_and_flow_are_rendered() -> None:
    menu = _blip_content(
        "application/json",
        {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "Pick a topic"},
                "action": {"sections": [{"rows": [{"id": "a", "title": "Billing"}]}]},
            },
        },
    )
    flow = _blip_content(
        "application/json",
        {
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": "Fill in the form"},
                "action": {"name": "flow", "parameters": {"flow_id": "9", "flow_cta": "Open form"}},
            },
        },
    )

    assert BridgeService._blip_content_as_text(menu) == "Pick a topic\n\nOptions: [Billing]"
    assert BridgeService._blip_content_as_text(flow) == (
        "[BLiP flow: flow]\n\nFill in the form\n\nOptions: [Open form]"
    )


def test_reaction_is_rendered_with_emoji_and_truncated_quote() -> None:
    long_quote = "x" * 300
    message = _blip_content(
        "application/vnd.lime.reaction+json",
        {
            "emoji": {"values": [0x2764, 0xFE0F]},
            "inReactionTo": {"id": "m0", "type": "text/plain", "value": long_quote},
        },
    )

    assert BridgeService._blip_content_as_text(message) == f"[Reaction: ❤️]\n> {'x' * 200}…"


def test_reaction_with_no_emoji_is_a_removal_and_ignores_invalid_code_points() -> None:
    removed = _blip_content("application/vnd.lime.reaction+json", {"emoji": {"values": []}})
    invalid = _blip_content(
        "application/vnd.lime.reaction+json",
        {"emoji": {"values": [0xD800, 0x110000, -1, True, 0x1F44D]}},
    )

    assert BridgeService._blip_content_as_text(removed) == "[Reaction removed]"
    assert BridgeService._blip_content_as_text(invalid) == "[Reaction: 👍]"


def test_desk_agent_label_is_decoded() -> None:
    message = _blip_content(
        "text/plain",
        "hi",
        **{
            "#messageEmitter": "Human",
            "#message.agentIdentity": "jane.doe%40example.com@blip.ai",
        },
    )

    assert BridgeService._outbound_label(message) == "BLiP agent: jane.doe@example.com"


def _media_event(settings, *, sender: str) -> InboundEvent:
    return _blip_event(
        "blip-media-1",
        **{
            "from": sender,
            "to": CUSTOMER if sender.startswith(settings.blip_bot_identity) else "mybot@msging.net",
            "type": "application/vnd.lime.media-link+json",
            "content": {
                "type": "application/pdf",
                "uri": "https://blipmediastore.blob.core.windows.net/m/a.pdf?sv=1&sig=SECRET",
                "title": "Contract.pdf",
                "text": "Contract.pdf",
            },
        },
    )


@pytest.mark.asyncio
async def test_agent_media_is_attached_instead_of_linked(session_factory, settings) -> None:
    chatwoot = _chatwoot_mock()
    media = AsyncMock(spec=MediaDownloader)
    media.download.return_value = DownloadedMedia("Contract.pdf", "application/pdf", b"%PDF")
    async with session_factory() as session:
        event = _media_event(settings, sender=f"{settings.blip_bot_identity}/router-1")
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
            media=media,
        )
        await service.process_blip_message(event)

        media.download.assert_awaited_once_with(
            "https://blipmediastore.blob.core.windows.net/m/a.pdf?sv=1&sig=SECRET",
            content_type="application/pdf",
            filename="Contract.pdf",
        )
        chatwoot.create_message.assert_awaited_once_with(
            conversation_id=200,
            content="[BLiP bot]",
            message_type="outgoing",
            content_attributes={"blip_message_id": "blip-media-1", "blip_direction": "outbound"},
            as_agent_bot=True,
            attachments=[("Contract.pdf", b"%PDF", "application/pdf")],
        )
        assert event.status == "processed"


@pytest.mark.asyncio
async def test_customer_media_is_attached_and_keeps_a_distinct_caption(
    session_factory, settings
) -> None:
    chatwoot = _chatwoot_mock()
    media = AsyncMock(spec=MediaDownloader)
    media.download.return_value = DownloadedMedia("photo.jpg", "image/jpeg", b"\xff\xd8")
    async with session_factory() as session:
        event = _media_event(settings, sender=CUSTOMER)
        event.payload["content"].update(type="image/jpeg", title="photo.jpg", text="My ID card")
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
            media=media,
        )
        await service.process_blip_message(event)

        chatwoot.create_message.assert_awaited_once_with(
            conversation_id=200,
            content="My ID card",
            message_type="incoming",
            content_attributes={"blip_message_id": "blip-media-1"},
            attachments=[("photo.jpg", b"\xff\xd8", "image/jpeg")],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_downloadable", "switched_off", "no_downloader"])
async def test_media_falls_back_to_a_link_without_the_signature(
    session_factory, settings, case
) -> None:
    chatwoot = _chatwoot_mock()
    media = AsyncMock(spec=MediaDownloader)
    media.download.return_value = None
    settings.blip_media_attachments = case != "switched_off"
    async with session_factory() as session:
        event = _media_event(settings, sender=f"{settings.blip_bot_identity}/router-1")
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
            media=None if case == "no_downloader" else media,
        )
        await service.process_blip_message(event)

        kwargs = chatwoot.create_message.await_args.kwargs
        assert "attachments" not in kwargs
        assert kwargs["content"] == (
            "[BLiP bot]\n[BLiP attachment (application/pdf): "
            "https://blipmediastore.blob.core.windows.net/m/a.pdf]\nContract.pdf"
        )
        assert event.status == "processed"


@pytest.mark.asyncio
async def test_transient_media_failure_is_left_for_the_worker_to_retry(
    session_factory, settings
) -> None:
    chatwoot = _chatwoot_mock()
    media = AsyncMock(spec=MediaDownloader)
    media.download.side_effect = IntegrationError("timed out", retryable=True)
    async with session_factory() as session:
        event = _media_event(settings, sender=f"{settings.blip_bot_identity}/router-1")
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
            media=media,
        )
        with pytest.raises(IntegrationError):
            await service.process_blip_message(event)

        chatwoot.create_message.assert_not_awaited()
        assert event.result_id is None


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("mybot@msging.net", True),
        ("mybot@msging.net/router-1", True),
        ("otherbot@msging.net", True),
        ("otherbot@msging.net/msging-application-router-5cd4d65d46-abcde", True),
        ("OtherBot@Msging.Net/x", True),
        (CUSTOMER, False),
        ("activecampaign:abc@broadcast.msging.net", False),
        ("user%40example.com@blip.ai", False),
        ("someone@example.com", False),
        ("msging.net", False),
        ("@msging.net", False),
        ("", False),
    ],
)
def test_bot_nodes_are_the_configured_bot_or_any_msging_net_application(identity, expected) -> None:
    assert is_bot_node(identity, "mybot@msging.net") is expected


def test_configured_bot_is_recognised_even_outside_the_msging_domain() -> None:
    assert is_bot_node("custom@bots.example/instance", "custom@bots.example") is True
    assert is_bot_node("custom@bots.example", "") is False


def _second_bot_event(external_id: str, **fields) -> InboundEvent:
    return _blip_event(
        external_id,
        **{"from": "otherbot@msging.net/router-5cd4d65d46-abcde", "to": CUSTOMER, **fields},
    )


@pytest.mark.asyncio
async def test_message_from_a_second_bot_is_mirrored_to_the_customer_not_as_a_new_customer(
    session_factory,
    settings,
) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _second_bot_event("other-out-1", content="Hi from the receptive bot")
        session.add(event)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )
        await service.process_blip_message(event)

        assert chatwoot.create_contact.await_args.kwargs["identifier"] == CUSTOMER
        chatwoot.create_message.assert_awaited_once_with(
            conversation_id=200,
            content="[BLiP bot: otherbot]\nHi from the receptive bot",
            message_type="outgoing",
            content_attributes={"blip_message_id": "other-out-1", "blip_direction": "outbound"},
            as_agent_bot=True,
        )
        mappings = (await session.scalars(select(ConversationMapping))).all()
        assert [m.blip_customer_identity for m in mappings] == [CUSTOMER]


@pytest.mark.asyncio
async def test_second_bot_and_first_bot_share_the_customers_conversation(
    session_factory,
    settings,
) -> None:
    chatwoot = _chatwoot_mock()
    chatwoot.create_message.side_effect = [{"id": 300}, {"id": 301}]
    async with session_factory() as session:
        session.add(_mapping(settings))
        first = _blip_event(
            "first-out",
            **{"from": f"{settings.blip_bot_identity}/router-1", "to": CUSTOMER, "content": "A"},
        )
        second = _second_bot_event("second-out", content="B")
        session.add_all([first, second])
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )

        await service.process_blip_message(first)
        await service.process_blip_message(second)

        contents = [call.kwargs["content"] for call in chatwoot.create_message.await_args_list]
        assert contents == ["[BLiP bot]\nA", "[BLiP bot: otherbot]\nB"]
        assert {
            call.kwargs["conversation_id"] for call in chatwoot.create_message.await_args_list
        } == {200}
        chatwoot.create_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_messages_between_two_bots_are_not_mirrored(session_factory, settings) -> None:
    chatwoot = _chatwoot_mock()
    async with session_factory() as session:
        event = _second_bot_event("bot-to-bot", to=settings.blip_bot_identity, content="handoff")
        session.add(event)
        await session.commit()
        service = BridgeService(
            session=session,
            settings=settings,
            blip=AsyncMock(spec=BlipClient),
            chatwoot=chatwoot,
        )

        await service.process_blip_message(event)

        assert event.status == "ignored"
        chatwoot.create_contact.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()


def test_labels_name_the_bot_only_when_it_is_not_the_configured_one() -> None:
    primary = _blip_content("text/plain", "hi")
    other = BlipMessage.model_validate(
        {
            "id": "m",
            "from": "otherbot@msging.net/router-1",
            "to": CUSTOMER,
            "type": "text/plain",
            "content": "x",
        }
    )

    assert BridgeService._outbound_label(primary, "mybot@msging.net") == "BLiP bot"
    assert BridgeService._outbound_label(other, "mybot@msging.net") == "BLiP bot: otherbot"
    assert BridgeService._outbound_label(other) == "BLiP bot"
