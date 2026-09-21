from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.services.bridge import BridgeService


@pytest.mark.asyncio
async def test_blip_message_creates_chatwoot_mapping_and_acknowledges(
    session_factory,
    settings,
) -> None:
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
