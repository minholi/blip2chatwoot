from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.models import ConversationMapping, InboundEvent
from app.services.bridge import BridgeService


@pytest.mark.asyncio
async def test_chatwoot_labels_are_mirrored_to_blip_ticket_tags(session_factory, settings) -> None:
    settings.blip_ticket_tag_sync_enabled = True
    blip = AsyncMock(spec=BlipClient)
    chatwoot = AsyncMock(spec=ChatwootClient)
    blip.get_ticket.return_value = {"id": "ticket-1", "tags": ["old"]}
    blip.get_active_tags.return_value = {"old", "vip"}

    async with session_factory() as session:
        mapping = ConversationMapping(
            blip_bot_identity=settings.blip_bot_identity,
            blip_customer_identity="551199999999@wa.gw.msging.net",
            chatwoot_account_id=settings.chatwoot_account_id,
            chatwoot_contact_id=100,
            chatwoot_source_id="source-1",
            chatwoot_conversation_id=200,
            blip_ticket_id="ticket-1",
            chatwoot_labels=["old"],
            blip_tags=["old"],
        )
        session.add(mapping)
        await session.commit()
        event = InboundEvent(
            provider="chatwoot",
            external_id="conversation_updated:200",
            event_type="conversation_updated",
            payload={
                "event": "conversation_updated",
                "id": 200,
                "labels": ["old", "vip"],
                "status": "open",
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

        blip.change_ticket_tags.assert_awaited_once_with("ticket-1", ["old", "vip"])
        saved = await session.scalar(select(ConversationMapping))
        assert saved is not None
        assert saved.blip_tags == ["old", "vip"]


@pytest.mark.asyncio
async def test_blip_ticket_tags_are_reconciled_to_chatwoot(session_factory, settings) -> None:
    settings.blip_ticket_tag_sync_enabled = True
    blip = AsyncMock(spec=BlipClient)
    chatwoot = AsyncMock(spec=ChatwootClient)
    blip.get_ticket.return_value = {"id": "ticket-1", "tags": ["old", "vip"]}
    chatwoot.get_conversation_labels.return_value = ["old"]

    async with session_factory() as session:
        mapping = ConversationMapping(
            blip_bot_identity=settings.blip_bot_identity,
            blip_customer_identity="551199999999@wa.gw.msging.net",
            chatwoot_account_id=settings.chatwoot_account_id,
            chatwoot_contact_id=100,
            chatwoot_source_id="source-1",
            chatwoot_conversation_id=200,
            blip_ticket_id="ticket-1",
            chatwoot_labels=["old"],
            blip_tags=["old"],
        )
        session.add(mapping)
        await session.commit()

        service = BridgeService(
            session=session,
            settings=settings,
            blip=blip,
            chatwoot=chatwoot,
        )
        await service.reconcile_blip_tags()

        chatwoot.ensure_labels.assert_awaited_once_with(["old", "vip"])
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200,
            labels=["old", "vip"],
        )
