from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.schemas import BlipMessage, BlipNotification
from app.services.queue import utcnow

logger = logging.getLogger(__name__)
_MAPPING_LOCKS: dict[str, asyncio.Lock] = {}


class BridgeService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        blip: BlipClient,
        chatwoot: ChatwootClient,
    ) -> None:
        self.session = session
        self.settings = settings
        self.blip = blip
        self.chatwoot = chatwoot

    async def process_blip_message(self, event: InboundEvent) -> None:
        message = BlipMessage.model_validate(event.payload)
        if not self._is_customer_message(message):
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        delivery = await self.session.scalar(
            select(MessageDelivery).where(MessageDelivery.blip_message_id == message.id)
        )
        if delivery:
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        lock = _MAPPING_LOCKS.setdefault(message.from_, asyncio.Lock())
        async with lock:
            mapping = await self._get_mapping(message.from_)
            if not mapping:
                mapping = await self._create_mapping(message.from_)
            elif mapping.status != "open":
                await self.chatwoot.toggle_status(
                    conversation_id=mapping.chatwoot_conversation_id,
                    status="open",
                )
                mapping.status = "open"
                await self.session.commit()

            await self._ensure_shadow_ticket(mapping)

            chatwoot_message_id = int(event.result_id) if event.result_id else None
            if chatwoot_message_id is None:
                content = self._blip_content_as_text(message)
                existing_message = await self._find_chatwoot_message(
                    conversation_id=mapping.chatwoot_conversation_id,
                    blip_message_id=message.id,
                )
                if existing_message:
                    chatwoot_message_id = self._required_int(
                        existing_message,
                        "id",
                        "Chatwoot message",
                    )
                else:
                    response = await self.chatwoot.create_message(
                        conversation_id=mapping.chatwoot_conversation_id,
                        content=content,
                        message_type="incoming",
                        content_attributes={"blip_message_id": message.id},
                    )
                    chatwoot_message_id = self._required_int(
                        response,
                        "id",
                        "Chatwoot message",
                    )
                event.result_id = str(chatwoot_message_id)
                event.status = "chatwoot_done"
                event.attempts += 1
                await self.session.commit()

            await self.blip.send_notification(
                message_id=message.id,
                to=message.from_,
                event="consumed",
            )
            event.status = "processed"
            event.processed_at = utcnow()
            await self.session.commit()

    async def process_chatwoot_event(self, event: InboundEvent) -> None:
        payload = event.payload
        event_name = str(payload.get("event", ""))
        conversation_id = self._conversation_id(payload)
        if conversation_id is None:
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        mapping = await self.session.scalar(
            select(ConversationMapping).where(
                ConversationMapping.chatwoot_account_id == self.settings.chatwoot_account_id,
                ConversationMapping.chatwoot_conversation_id == conversation_id,
            )
        )
        if not mapping:
            logger.info("Ignoring Chatwoot event for unmapped conversation %s", conversation_id)
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        if event_name == "message_created":
            await self._process_chatwoot_message(mapping, payload)
        elif event_name in {"conversation_updated", "conversation_status_changed"}:
            await self._process_chatwoot_conversation_update(mapping, payload)
        else:
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        event.status = "processed"
        event.processed_at = utcnow()
        await self.session.commit()

    async def process_blip_notification(self, event: InboundEvent) -> None:
        notification = BlipNotification.model_validate(event.payload)
        delivery = await self.session.scalar(
            select(MessageDelivery).where(MessageDelivery.blip_message_id == notification.id)
        )
        if not delivery or not delivery.chatwoot_message_id:
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        mapping = await self.session.get(ConversationMapping, delivery.mapping_id)
        if not mapping:
            event.status = "ignored"
            event.processed_at = utcnow()
            await self.session.commit()
            return

        status = self._chatwoot_status_for_blip_event(notification.event)
        if status and not self._is_monotonic_delivery_status(delivery.status, status):
            status = None
        if status:
            await self.chatwoot.update_message_status(
                conversation_id=mapping.chatwoot_conversation_id,
                message_id=delivery.chatwoot_message_id,
                status=status,
                external_error=(notification.reason or {}).get("description"),
            )
            delivery.status = status
            await self.session.commit()

        event.status = "processed"
        event.processed_at = utcnow()
        await self.session.commit()

    async def reconcile_blip_tags(self) -> None:
        if not self.settings.blip_ticket_tag_sync_enabled:
            return
        mappings = list((await self.session.scalars(select(ConversationMapping))).all())
        for mapping in mappings:
            try:
                await self._ensure_shadow_ticket(mapping)
                await self._reconcile_mapping_tags(mapping)
            except IntegrationError:
                logger.exception("Failed to reconcile tags for mapping %s", mapping.id)

    async def _process_chatwoot_message(
        self,
        mapping: ConversationMapping,
        payload: dict[str, Any],
    ) -> None:
        if not self._is_public_agent_message(payload):
            return
        message_id = self._optional_int(payload.get("id"))
        if message_id is None:
            return

        delivery = await self.session.scalar(
            select(MessageDelivery).where(MessageDelivery.chatwoot_message_id == message_id)
        )
        if delivery and delivery.status in {"sent", "delivered", "read", "failed"}:
            if delivery.status != "failed":
                await self.chatwoot.update_message_status(
                    conversation_id=mapping.chatwoot_conversation_id,
                    message_id=message_id,
                    status=delivery.status,
                )
            return

        blip_message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"chatwoot:{self.settings.chatwoot_account_id}:{message_id}",
            )
        )
        if not delivery:
            delivery = MessageDelivery(
                mapping_id=mapping.id,
                chatwoot_message_id=message_id,
                blip_message_id=blip_message_id,
                status="sending",
            )
            self.session.add(delivery)
            await self.session.commit()

        content = self._chatwoot_content_as_text(payload)
        await self.blip.send_message(
            to=mapping.blip_customer_identity,
            message_type="text/plain",
            content=content,
            message_id=delivery.blip_message_id,
        )
        delivery.status = "sent"
        delivery.last_error = None
        await self.session.commit()

        await self.chatwoot.update_message_status(
            conversation_id=mapping.chatwoot_conversation_id,
            message_id=message_id,
            status="sent",
        )

    async def _find_chatwoot_message(
        self,
        *,
        conversation_id: int,
        blip_message_id: str,
    ) -> dict[str, Any] | None:
        for message in await self.chatwoot.get_messages(conversation_id=conversation_id):
            attributes = message.get("content_attributes") or {}
            if attributes.get("blip_message_id") == blip_message_id:
                return message
        return None

    async def _process_chatwoot_conversation_update(
        self,
        mapping: ConversationMapping,
        payload: dict[str, Any],
    ) -> None:
        labels = self._string_list(payload.get("labels"))
        previous_labels = list(mapping.chatwoot_labels)
        mapping.chatwoot_labels = labels
        status = payload.get("status")
        if isinstance(status, str):
            mapping.status = status

        if self.settings.blip_ticket_tag_sync_enabled and labels != previous_labels:
            await self._sync_chatwoot_labels_to_blip(mapping, previous_labels, labels)
        await self.session.commit()

    async def _sync_chatwoot_labels_to_blip(
        self,
        mapping: ConversationMapping,
        previous_labels: list[str],
        labels: list[str],
    ) -> None:
        await self._ensure_shadow_ticket(mapping)
        if not mapping.blip_ticket_id:
            return
        ticket = await self.blip.get_ticket(mapping.blip_ticket_id)
        current_tags = self._string_list(ticket.get("tags"))
        unmanaged_tags = [tag for tag in current_tags if tag not in previous_labels]
        active_tags = await self.blip.get_active_tags()
        synchronized_labels = [label for label in labels if label in active_tags]
        desired_tags = self._unique(unmanaged_tags + synchronized_labels)
        if desired_tags != current_tags:
            await self.blip.change_ticket_tags(mapping.blip_ticket_id, desired_tags)
        mapping.blip_tags = desired_tags

    async def _reconcile_mapping_tags(self, mapping: ConversationMapping) -> None:
        if not mapping.blip_ticket_id:
            return
        ticket = await self.blip.get_ticket(mapping.blip_ticket_id)
        current_tags = self._string_list(ticket.get("tags"))
        if current_tags == list(mapping.blip_tags):
            return

        current_labels = await self.chatwoot.get_conversation_labels(
            conversation_id=mapping.chatwoot_conversation_id
        )
        unmanaged_labels = [label for label in current_labels if label not in mapping.blip_tags]
        desired_labels = self._unique(unmanaged_labels + current_tags)
        await self.chatwoot.ensure_labels(current_tags)
        await self.chatwoot.add_conversation_labels(
            conversation_id=mapping.chatwoot_conversation_id,
            labels=desired_labels,
        )
        mapping.chatwoot_labels = desired_labels
        mapping.blip_tags = current_tags
        await self.session.commit()

    async def _create_mapping(self, customer_identity: str) -> ConversationMapping:
        phone_number = self._phone_number_from_identity(customer_identity)
        contact = await self.chatwoot.create_contact(
            name=phone_number or customer_identity,
            identifier=customer_identity,
            phone_number=phone_number,
        )
        contact_resource = self._resource_dict(contact, "contact")
        contact_id = self._required_int(contact_resource, "id", "Chatwoot contact")
        nested_payload = contact.get("payload")
        if not isinstance(nested_payload, dict):
            nested_payload = {}
        contact_inboxes = (
            contact_resource.get("contact_inboxes")
            or contact_resource.get("contact_inbox")
            or contact.get("contact_inboxes")
            or contact.get("contact_inbox")
            or nested_payload.get("contact_inboxes")
            or nested_payload.get("contact_inbox")
            or []
        )
        if isinstance(contact_inboxes, dict):
            contact_inboxes = [contact_inboxes]
        source_id = next(
            (
                str(item.get("source_id"))
                for item in contact_inboxes
                if isinstance(item, dict)
                and item.get("source_id")
                and self._optional_int((item.get("inbox") or {}).get("id"))
                == self.settings.chatwoot_inbox_id
            ),
            None,
        )
        if not source_id:
            raise IntegrationError(
                "Chatwoot did not return an API inbox source id",
                retryable=False,
            )

        conversation = await self.chatwoot.create_conversation(
            source_id=source_id,
            contact_id=contact_id,
        )
        conversation_id = self._required_int(conversation, "id", "Chatwoot conversation")
        mapping = ConversationMapping(
            blip_bot_identity=self.settings.blip_bot_identity,
            blip_customer_identity=customer_identity,
            chatwoot_account_id=self.settings.chatwoot_account_id,
            chatwoot_contact_id=contact_id,
            chatwoot_source_id=source_id,
            chatwoot_conversation_id=conversation_id,
        )
        self.session.add(mapping)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._get_mapping(customer_identity)
            if existing:
                return existing
            raise
        return mapping

    async def _ensure_shadow_ticket(self, mapping: ConversationMapping) -> None:
        if not self.settings.blip_ticket_tag_sync_enabled or mapping.blip_ticket_id:
            return
        try:
            mapping.blip_ticket_id = await self.blip.create_shadow_ticket(
                mapping.blip_customer_identity
            )
            await self.session.commit()
        except IntegrationError:
            logger.exception(
                "Could not create optional BLiP shadow ticket for mapping %s",
                mapping.id,
            )

    async def _get_mapping(self, customer_identity: str) -> ConversationMapping | None:
        return await self.session.scalar(
            select(ConversationMapping).where(
                ConversationMapping.blip_bot_identity == self.settings.blip_bot_identity,
                ConversationMapping.blip_customer_identity == customer_identity,
            )
        )

    def _is_customer_message(self, message: BlipMessage) -> bool:
        sender = message.from_
        bot_identity = self.settings.blip_bot_identity
        if not sender or sender.startswith("postmaster@"):
            return False
        if bot_identity and (sender == bot_identity or sender.startswith(f"{bot_identity}/")):
            return False
        return "@" in sender

    @staticmethod
    def _is_public_agent_message(payload: dict[str, Any]) -> bool:
        if payload.get("message_type") != "outgoing" or payload.get("private") is True:
            return False
        sender_type = str((payload.get("sender") or {}).get("type", "")).lower()
        return sender_type == "user"

    @staticmethod
    def _blip_content_as_text(message: BlipMessage) -> str:
        if message.type == "text/plain":
            return str(message.content)
        if isinstance(message.content, dict):
            uri = message.content.get("uri")
            if uri:
                return f"[BLiP attachment: {uri}]"
        try:
            serialized = json.dumps(message.content, ensure_ascii=False, default=str)
        except TypeError:
            serialized = str(message.content)
        return f"[BLiP message type: {message.type}]\n{serialized}"

    @staticmethod
    def _chatwoot_content_as_text(payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content
        attachments = payload.get("attachments") or []
        urls = [
            str(item.get("data_url") or item.get("url"))
            for item in attachments
            if isinstance(item, dict) and (item.get("data_url") or item.get("url"))
        ]
        if urls:
            return "\n".join(f"[Chatwoot attachment: {url}]" for url in urls)
        return "[Chatwoot message without text]"

    @staticmethod
    def _conversation_id(payload: dict[str, Any]) -> int | None:
        conversation = payload.get("conversation") or {}
        conversation_id = conversation.get("id") or payload.get("conversation_id")
        if conversation_id is None and payload.get("event") in {
            "conversation_updated",
            "conversation_status_changed",
        }:
            conversation_id = payload.get("id")
        return BridgeService._optional_int(conversation_id)

    @staticmethod
    def _chatwoot_status_for_blip_event(event: str) -> str | None:
        return {
            "accepted": "sent",
            "dispatched": "sent",
            "received": "delivered",
            "consumed": "read",
            "failed": "failed",
        }.get(event)

    @staticmethod
    def _is_monotonic_delivery_status(current: str, new: str) -> bool:
        ranks = {"sending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
        if current == "failed":
            return False
        return ranks.get(new, -1) >= ranks.get(current, -1)

    @staticmethod
    def _resource_dict(payload: dict[str, Any], resource_name: str) -> dict[str, Any]:
        direct = payload.get(resource_name)
        if isinstance(direct, dict):
            return direct
        nested = payload.get("payload")
        if isinstance(nested, dict):
            nested_resource = nested.get(resource_name)
            if isinstance(nested_resource, dict):
                return nested_resource
            return nested
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            return nested[0]
        return payload

    @staticmethod
    def _required_int(payload: dict[str, Any], key: str, resource_name: str) -> int:
        value = BridgeService._optional_int(payload.get(key))
        if value is None:
            nested = payload.get("payload")
            if isinstance(nested, dict):
                value = BridgeService._optional_int(nested.get(key))
        if value is None:
            raise IntegrationError(
                f"{resource_name} response did not include {key}",
                retryable=False,
            )
        return value

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _phone_number_from_identity(identity: str) -> str | None:
        local, _, domain = identity.partition("@")
        if domain.startswith("wa.") and local.isdigit():
            return f"+{local}"
        return None

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
            return []
        return [str(item) for item in value if str(item).strip()]

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
