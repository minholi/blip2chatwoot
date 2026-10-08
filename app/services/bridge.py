from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.integrations.media import DownloadedMedia, MediaDownloader
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.schemas import BlipMessage, BlipNotification
from app.services.contact_profile import PHONE_LIKE, latest_contact_event
from app.services.queue import utcnow

logger = logging.getLogger(__name__)
_MAPPING_LOCKS: dict[str, asyncio.Lock] = {}
# Chatwoot agent ids by (account, e-mail), so a Desk agent is looked up or created once per worker.
_AGENT_LOCKS: dict[str, asyncio.Lock] = {}
_AGENT_USER_IDS: dict[tuple[int, str], int] = {}
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ATTENDANT_REFRESH_SECONDS = 600


@dataclass
class _AttendantDirectory:
    """BLiP Desk operator names by e-mail, loaded on demand and refreshed on a miss."""

    names: dict[str, str] = field(default_factory=dict)
    loaded_at: float | None = None


_ATTENDANTS = _AttendantDirectory()
_DESK_CONTENT_TYPE_PREFIX = "application/vnd.iris."
_BOT_NODE_DOMAIN = "msging.net"
_REACTION_QUOTE_LIMIT = 200
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")


def is_bot_node(identity: str, primary: str = "") -> bool:
    """Whether a BLiP node is a bot: the configured one or any `<name>@msging.net[/instance]`.

    Customers live on channel gateways (`<id>@wa.gw.msging.net`, `@broadcast.msging.net`, ...),
    never on the bare `msging.net` domain, so a second bot of the same contract (e.g. a receptive
    bot) is recognised without configuration instead of being mirrored as a customer named after it.
    """
    node = identity.partition("/")[0]
    if primary and node == primary.partition("/")[0]:
        return True
    name, _, domain = node.rpartition("@")
    return bool(name) and domain.lower() == _BOT_NODE_DOMAIN


class BridgeService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        blip: BlipClient,
        chatwoot: ChatwootClient,
        media: MediaDownloader | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.blip = blip
        self.chatwoot = chatwoot
        self.media = media

    async def process_blip_message(self, event: InboundEvent) -> None:
        message = BlipMessage.model_validate(event.payload)
        route = self._route_message(message)
        if route is None:
            await self._mark_ignored(event)
            return
        direction, customer_identity = route

        delivery = await self.session.scalar(
            select(MessageDelivery).where(MessageDelivery.blip_message_id == message.id)
        )
        if delivery:
            await self._mark_ignored(event)
            return

        lock = _MAPPING_LOCKS.setdefault(customer_identity, asyncio.Lock())
        async with lock:
            mapping = await self._get_mapping(customer_identity)
            if not mapping:
                mapping = await self._create_mapping(customer_identity)
            elif direction == "inbound" and mapping.status != "open":
                await self.chatwoot.toggle_status(
                    conversation_id=mapping.chatwoot_conversation_id,
                    status="open",
                )
                mapping.status = "open"
                await self.session.commit()

            await self._ensure_shadow_ticket(mapping)

            chatwoot_message_id = int(event.result_id) if event.result_id else None
            if chatwoot_message_id is None:
                chatwoot_message_id = await self._mirror_message(mapping, message, direction)
                if direction == "outbound":
                    # Recorded in the same commit as result_id so the Chatwoot echo of this
                    # message is recognised as ours and never forwarded back to BLiP.
                    self.session.add(
                        MessageDelivery(
                            mapping_id=mapping.id,
                            chatwoot_message_id=chatwoot_message_id,
                            blip_message_id=message.id,
                            status="mirrored",
                        )
                    )
                event.result_id = str(chatwoot_message_id)
                event.status = "chatwoot_done"
                event.attempts += 1
                await self.session.commit()

            if direction == "outbound":
                await self._sync_agent_assignment(mapping, message)

            if direction == "inbound" and self.settings.blip_ack_messages:
                await self.blip.send_notification(
                    message_id=message.id,
                    to=message.from_,
                    event="consumed",
                )
            event.status = "processed"
            event.processed_at = utcnow()
            await self.session.commit()

    async def _mirror_message(
        self,
        mapping: ConversationMapping,
        message: BlipMessage,
        direction: str,
    ) -> int:
        existing_message = await self._find_chatwoot_message(
            conversation_id=mapping.chatwoot_conversation_id,
            blip_message_id=message.id,
        )
        if existing_message:
            return self._required_int(existing_message, "id", "Chatwoot message")

        content = self._blip_content_as_text(message)
        extra: dict[str, Any] = {}
        media = await self._fetch_blip_media(message)
        if media:
            # The file is attached, so the text keeps only the captions that add something.
            captions = self._blip_media_captions(message.content)
            content = "\n".join(caption for caption in captions if caption != media.filename)
            extra["attachments"] = [(media.filename, media.data, media.content_type)]
        if direction == "inbound":
            response = await self.chatwoot.create_message(
                conversation_id=mapping.chatwoot_conversation_id,
                content=content,
                message_type="incoming",
                content_attributes={"blip_message_id": message.id},
                **extra,
            )
        else:
            label = f"[{self._outbound_label(message, self.settings.blip_bot_identity)}]"
            response = await self.chatwoot.create_message(
                conversation_id=mapping.chatwoot_conversation_id,
                content=f"{label}\n{content}" if content else label,
                message_type="outgoing",
                content_attributes={"blip_message_id": message.id, "blip_direction": "outbound"},
                as_agent_bot=True,
                **extra,
            )
        return self._required_int(response, "id", "Chatwoot message")

    async def _fetch_blip_media(self, message: BlipMessage) -> DownloadedMedia | None:
        """The file behind a BLiP media link, or None to keep the text placeholder instead."""
        content = message.content
        if (
            self.media is None
            or not self.settings.blip_media_attachments
            or message.type != "application/vnd.lime.media-link+json"
            or not isinstance(content, dict)
            or not isinstance(content.get("uri"), str)
        ):
            return None
        title = content.get("title")
        media_type = content.get("type")
        return await self.media.download(
            content["uri"],
            content_type=media_type if isinstance(media_type, str) else None,
            filename=title if isinstance(title, str) else None,
        )

    async def _mark_ignored(self, event: InboundEvent) -> None:
        event.status = "ignored"
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
        if not self.settings.chatwoot_replies_to_blip:
            return
        if not self._is_public_agent_message(payload):
            return
        message_id = self._optional_int(payload.get("id"))
        if message_id is None:
            return

        delivery = await self.session.scalar(
            select(MessageDelivery).where(MessageDelivery.chatwoot_message_id == message_id)
        )
        if delivery and delivery.status == "mirrored":
            return
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
            status=str(conversation.get("status") or "open"),
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
        await self._open_pending_conversation(mapping)
        await self._apply_deferred_contact_profile(mapping)
        return mapping

    async def process_blip_contact(self, event: InboundEvent) -> None:
        mapping = await self._get_mapping(str(event.payload.get("identity", "")))
        if mapping is None:
            # Applied by `_create_mapping` once the customer has a conversation.
            event.status = "deferred"
        else:
            await self._apply_contact_profile(mapping, event)
            event.status = "processed"
        event.processed_at = utcnow()
        await self.session.commit()

    async def _apply_deferred_contact_profile(self, mapping: ConversationMapping) -> None:
        """Best effort: a profile problem must never block delivering the customer's message."""
        event = await latest_contact_event(self.session, mapping.blip_customer_identity)
        if event is None or event.status != "deferred":
            return
        try:
            await self._apply_contact_profile(mapping, event)
        except IntegrationError as exc:
            logger.warning(
                "Could not apply the stored BLiP profile to Chatwoot contact %s: %s",
                mapping.chatwoot_contact_id,
                exc,
            )
            return
        event.status = "processed"
        event.processed_at = utcnow()
        await self.session.commit()

    async def _apply_contact_profile(
        self,
        mapping: ConversationMapping,
        event: InboundEvent,
    ) -> None:
        profile = event.payload
        contact_id = mapping.chatwoot_contact_id
        contact = self._resource_dict(
            await self.chatwoot.get_contact(contact_id=contact_id), "contact"
        )
        changes: dict[str, Any] = {}

        name = profile.get("name")
        current_name = contact.get("name")
        if (
            isinstance(name, str)
            and name != current_name
            and await self._may_rename_contact(mapping, event, current_name)
        ):
            changes["name"] = name
        email = profile.get("email")
        if isinstance(email, str) and email.lower() != str(contact.get("email") or "").lower():
            changes["email"] = email
        wanted = profile.get("custom_attributes")
        current = contact.get("custom_attributes")
        current = current if isinstance(current, dict) else {}
        if isinstance(wanted, dict) and (merged := {**current, **wanted}) != current:
            changes["custom_attributes"] = merged
        if not changes:
            return

        try:
            await self.chatwoot.update_contact(contact_id=contact_id, **changes)
        except IntegrationError as exc:
            if exc.status_code != 422 or "email" not in changes:
                raise
            # Chatwoot keeps e-mails unique per account: still update the rest of the profile.
            logger.warning("Chatwoot rejected the e-mail of contact %s", contact_id)
            del changes["email"]
            if changes:
                await self.chatwoot.update_contact(contact_id=contact_id, **changes)

    async def _may_rename_contact(
        self,
        mapping: ConversationMapping,
        event: InboundEvent,
        current_name: Any,
    ) -> bool:
        """Rename only placeholders and names this bridge set, never one an agent typed."""
        if not isinstance(current_name, str) or not current_name.strip():
            return True
        if current_name == mapping.blip_customer_identity or PHONE_LIKE.match(current_name):
            return True
        previous = await latest_contact_event(
            self.session,
            mapping.blip_customer_identity,
            before_id=event.id,
            status="processed",
        )
        return previous is not None and previous.payload.get("name") == current_name

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

    async def _open_pending_conversation(self, mapping: ConversationMapping) -> None:
        """Open a conversation Chatwoot created as `pending`.

        With an Agent Bot on the inbox Chatwoot creates conversations as `pending` whatever status
        was requested, which hides them from the agents' open list. A failure leaves the mapping
        `pending`, so the customer's next message opens it through the usual reopen path.
        """
        if mapping.status != "pending":
            return
        try:
            await self.chatwoot.toggle_status(
                conversation_id=mapping.chatwoot_conversation_id,
                status="open",
            )
        except IntegrationError as exc:
            logger.warning(
                "Could not open new Chatwoot conversation %s (HTTP %s)",
                mapping.chatwoot_conversation_id,
                exc.status_code,
            )
            return
        mapping.status = "open"
        await self.session.commit()

    async def _sync_agent_assignment(
        self,
        mapping: ConversationMapping,
        message: BlipMessage,
    ) -> None:
        """Assign the conversation to the Desk agent who just wrote, creating their Chatwoot user.

        Only acts when the agent differs from the one last assigned, so a manual reassignment in
        Chatwoot stands until the BLiP agent changes. Best effort like the shadow ticket: the
        message is already mirrored, so a failure is logged and retried on the agent's next message.
        """
        if (
            not self.settings.chatwoot_agent_sync
            or message.metadata.get("#messageEmitter") != "Human"
        ):
            return
        agent = message.metadata.get("#message.agentIdentity")
        email = self._agent_display_name(agent).strip().lower() if agent else ""
        if len(email) > 255 or not _EMAIL_SHAPE.match(email):
            logger.info("Desk agent identity is not an e-mail; conversation not assigned")
            return
        if mapping.blip_agent_identity == email:
            return
        try:
            user_id = await self._resolve_chatwoot_agent(email)
            await self.chatwoot.assign_conversation(
                conversation_id=mapping.chatwoot_conversation_id,
                assignee_id=user_id,
            )
        except IntegrationError as exc:
            # The user may have been deleted in Chatwoot; look it up again next time.
            _AGENT_USER_IDS.pop((self.settings.chatwoot_account_id, email), None)
            logger.warning(
                "Could not assign Chatwoot conversation %s to its BLiP agent (HTTP %s)",
                mapping.chatwoot_conversation_id,
                exc.status_code,
            )
            return
        mapping.blip_agent_identity = email
        await self.session.commit()

    async def _resolve_chatwoot_agent(self, email: str) -> int:
        key = (self.settings.chatwoot_account_id, email)
        if key in _AGENT_USER_IDS:
            return _AGENT_USER_IDS[key]
        async with _AGENT_LOCKS.setdefault(email, asyncio.Lock()):
            if key in _AGENT_USER_IDS:
                return _AGENT_USER_IDS[key]
            user_id = await self._find_chatwoot_agent(email)
            if user_id is None:
                try:
                    created = await self.chatwoot.create_agent(
                        name=await self._agent_full_name(email),
                        email=email,
                    )
                except IntegrationError as exc:
                    if exc.status_code != 422:
                        raise
                    # Already taken: created elsewhere since the lookup above.
                    user_id = await self._find_chatwoot_agent(email)
                    if user_id is None:
                        raise
                else:
                    user_id = self._required_int(created, "id", "Chatwoot agent")
            # Additive and idempotent. An agent outside the inbox can be assigned a conversation
            # but cannot see it, which also holds for agents that already existed in Chatwoot.
            await self.chatwoot.add_inbox_agents([user_id])
            _AGENT_USER_IDS[key] = user_id
            return user_id

    async def _find_chatwoot_agent(self, email: str) -> int | None:
        for agent in await self.chatwoot.list_agents():
            agent_id = agent.get("id")
            if str(agent.get("email", "")).lower() == email and isinstance(agent_id, int):
                return agent_id
        return None

    async def _agent_full_name(self, email: str) -> str:
        """The operator's name in BLiP Desk, or one derived from the e-mail when unavailable."""
        name = None
        if self.settings.blip_agent_name_lookup:
            name = await self._desk_attendant_name(email)
        return name or self._agent_name_from_email(email)

    async def _desk_attendant_name(self, email: str) -> str | None:
        now = time.monotonic()
        loaded_at = _ATTENDANTS.loaded_at
        stale = loaded_at is None or now - loaded_at > _ATTENDANT_REFRESH_SECONDS
        if email not in _ATTENDANTS.names and stale:
            try:
                attendants = await self.blip.get_attendants()
            except IntegrationError as exc:
                if exc.retryable:
                    # Transient: wait for the agent's next message instead of creating the Chatwoot
                    # user with a guessed name, which would stay there.
                    raise
                logger.warning(
                    "Could not read BLiP Desk attendants (HTTP %s); naming the agent by e-mail",
                    exc.status_code,
                )
                _ATTENDANTS.loaded_at = now
                return None
            _ATTENDANTS.names = self._attendant_names(attendants)
            _ATTENDANTS.loaded_at = now
        return _ATTENDANTS.names.get(email)

    @staticmethod
    def _attendant_names(attendants: list[dict[str, Any]]) -> dict[str, str]:
        """Full names keyed by e-mail, from both the ``email`` field and the Desk identity."""
        names: dict[str, str] = {}
        for attendant in attendants:
            full_name = str(attendant.get("fullName") or "").strip()
            identity = attendant.get("identity")
            addresses = [
                attendant.get("email"),
                BridgeService._agent_display_name(identity) if identity else None,
            ]
            for address in addresses:
                if address and full_name:
                    names[str(address).strip().lower()] = full_name
        return names

    @staticmethod
    def _agent_name_from_email(email: str) -> str:
        """``maria.silva@example.com`` -> ``Maria Silva``."""
        words = re.split(r"[._+\-\s]+", email.partition("@")[0])
        return " ".join(word.capitalize() for word in words if word) or email

    async def _get_mapping(self, customer_identity: str) -> ConversationMapping | None:
        return await self.session.scalar(
            select(ConversationMapping).where(
                ConversationMapping.blip_bot_identity == self.settings.blip_bot_identity,
                ConversationMapping.blip_customer_identity == customer_identity,
            )
        )

    def _route_message(self, message: BlipMessage) -> tuple[str, str] | None:
        """Return (direction, customer identity), or None when the message is not mirrored."""
        if self._is_non_content(message):
            return None
        sender = message.from_
        if not sender or sender.startswith("postmaster@"):
            return None
        if self._is_bot_node(sender):
            recipient = message.to
            if (
                not recipient
                or "@" not in recipient
                or recipient.startswith("postmaster@")
                or self._is_bot_node(recipient)
            ):
                return None
            return "outbound", recipient
        return ("inbound", sender) if "@" in sender else None

    def _is_bot_node(self, identity: str) -> bool:
        return is_bot_node(identity, self.settings.blip_bot_identity)

    @staticmethod
    def _is_non_content(message: BlipMessage) -> bool:
        if message.type.startswith(_DESK_CONTENT_TYPE_PREFIX):
            return True
        if message.type == "application/vnd.lime.chatstate+json":
            return True
        return (
            message.type == "application/json"
            and isinstance(message.content, dict)
            and "typing_indicator" in message.content
        )

    @staticmethod
    def _outbound_label(message: BlipMessage, primary_bot: str = "") -> str:
        """Prefix naming who sent the message: the Desk agent's e-mail, or the bot."""
        if message.metadata.get("#messageEmitter") == "Human":
            agent = message.metadata.get("#message.agentIdentity")
            return (
                f"BLiP agent: {BridgeService._agent_display_name(agent)}" if agent else "BLiP agent"
            )
        node = message.from_.partition("/")[0]
        if primary_bot and node != primary_bot.partition("/")[0]:
            # Another bot of the same contract: say which one, since it isn't the configured bot.
            return f"BLiP bot: {node.partition('@')[0]}"
        return "BLiP bot"

    @staticmethod
    def _agent_display_name(identity: Any) -> str:
        """Desk agents arrive as ``user%40example.com@blip.ai``; show the plain e-mail."""
        name = unquote(str(identity))
        return name.removesuffix("@blip.ai")

    @staticmethod
    def _is_public_agent_message(payload: dict[str, Any]) -> bool:
        if payload.get("message_type") != "outgoing" or payload.get("private") is True:
            return False
        if (payload.get("content_attributes") or {}).get("blip_message_id"):
            return False
        sender_type = str((payload.get("sender") or {}).get("type", "")).lower()
        return sender_type == "user"

    @staticmethod
    def _blip_content_as_text(message: BlipMessage) -> str:
        if message.type == "text/plain":
            return str(message.content)
        if message.type == "application/vnd.lime.reply+json":
            reply = BridgeService._blip_reply_as_text(message.content)
            if reply:
                return reply
        if message.type == "application/vnd.lime.reaction+json":
            reaction = BridgeService._blip_reaction_as_text(message.content)
            if reaction:
                return reaction
        if message.type == "application/json":
            rendered = BridgeService._blip_template_as_text(
                message.content
            ) or BridgeService._blip_interactive_as_text(message.content)
            if rendered:
                return rendered
        if isinstance(message.content, dict):
            uri = message.content.get("uri")
            if uri:
                media_type = message.content.get("type")
                kind = f" ({media_type})" if isinstance(media_type, str) and media_type else ""
                captions = BridgeService._blip_media_captions(message.content)
                return "\n".join(
                    [
                        f"[BLiP attachment{kind}: {BridgeService._without_signature(str(uri))}]",
                        *captions,
                    ]
                )
        try:
            serialized = json.dumps(message.content, ensure_ascii=False, default=str)
        except TypeError:
            serialized = str(message.content)
        return f"[BLiP message type: {message.type}]\n{serialized}"

    @staticmethod
    def _blip_reply_as_text(content: Any) -> str | None:
        if not isinstance(content, dict):
            return None
        replied = content.get("replied")
        if (
            not isinstance(replied, dict)
            or replied.get("type") != "text/plain"
            or not isinstance(replied.get("value"), str)
        ):
            return None
        in_reply_to = content.get("inReplyTo")
        if not isinstance(in_reply_to, dict):
            return replied["value"]
        quoted = in_reply_to.get("value")
        if in_reply_to.get("type") != "text/plain" or not isinstance(quoted, str):
            return replied["value"]
        quote = "\n".join(f"> {line}" for line in quoted.strip().splitlines())
        return f"{quote}\n\n{replied['value']}" if quote else replied["value"]

    @staticmethod
    def _blip_media_captions(content: dict[str, Any]) -> list[str]:
        return list(
            dict.fromkeys(str(content[key]) for key in ("title", "text") if content.get(key))
        )

    @staticmethod
    def _without_signature(uri: str) -> str:
        """Drop the query of a signed (SAS) URL: it expires in minutes and embeds a credential."""
        try:
            parts = urlsplit(uri)
        except ValueError:
            return uri.partition("?")[0]
        if not any(key.lower() == "sig" for key, _ in parse_qsl(parts.query)):
            return uri
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @staticmethod
    def _blip_template_as_text(content: Any) -> str | None:
        """WhatsApp template: body with ``{{n}}`` filled from the send parameters, plus buttons."""
        template = content.get("templateContent") if isinstance(content, dict) else None
        if not isinstance(template, dict):
            return None
        parameters = BridgeService._template_body_parameters(content.get("template"))

        def fill(match: re.Match[str]) -> str:
            index = int(match.group(1)) - 1
            value = parameters[index] if 0 <= index < len(parameters) else None
            return value if value is not None else match.group(0)

        blocks: list[str] = []
        buttons: list[str] = []
        for component in template.get("components") or []:
            if not isinstance(component, dict):
                continue
            kind = str(component.get("type", "")).upper()
            text = component.get("text")
            if kind in {"HEADER", "BODY", "FOOTER"} and isinstance(text, str):
                # BLiP double-escapes line breaks in template text: it arrives as a literal "\n".
                text = text.replace("\\n", "\n").strip()
                if text:
                    blocks.append(_TEMPLATE_PLACEHOLDER.sub(fill, text))
            elif kind == "BUTTONS":
                buttons.extend(BridgeService._button_labels(component.get("buttons")))
        if not blocks:
            return None
        if buttons:
            blocks.append(f"Options: {' '.join(buttons)}")
        name = template.get("name")
        title = f"[BLiP template: {name}]" if isinstance(name, str) and name else "[BLiP template]"
        return "\n\n".join([title, *blocks])

    @staticmethod
    def _template_body_parameters(template: Any) -> list[str | None]:
        if not isinstance(template, dict):
            return []
        for component in template.get("components") or []:
            if isinstance(component, dict) and str(component.get("type", "")).lower() == "body":
                return [
                    item["text"] if isinstance(item.get("text"), str) else None
                    for item in component.get("parameters") or []
                    if isinstance(item, dict)
                ]
        return []

    @staticmethod
    def _blip_interactive_as_text(content: Any) -> str | None:
        """WhatsApp interactive message: body text plus the reply buttons / list rows offered."""
        interactive = content.get("interactive") if isinstance(content, dict) else None
        if not isinstance(interactive, dict):
            return None
        action = interactive.get("action")
        action = action if isinstance(action, dict) else {}
        texts: list[str] = []
        for part in ("header", "body", "footer"):
            section = interactive.get(part)
            text = section.get("text") if isinstance(section, dict) else None
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        options = BridgeService._button_labels(
            [
                (button.get("reply") or button) if isinstance(button, dict) else None
                for button in action.get("buttons") or []
            ]
        )
        for section in action.get("sections") or []:
            if isinstance(section, dict):
                options.extend(BridgeService._button_labels(section.get("rows")))
        parameters = action.get("parameters")
        flow_cta = parameters.get("flow_cta") if isinstance(parameters, dict) else None
        if isinstance(flow_cta, str) and flow_cta:
            options.append(f"[{flow_cta}]")
        if not texts and not options:
            return None
        blocks = []
        if interactive.get("type") == "flow" and isinstance(action.get("name"), str):
            blocks.append(f"[BLiP flow: {action['name']}]")
        blocks.extend(texts)
        if options:
            blocks.append(f"Options: {' '.join(options)}")
        return "\n\n".join(blocks)

    @staticmethod
    def _button_labels(items: Any) -> list[str]:
        labels = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            label = item.get("title") or item.get("text")
            if isinstance(label, str) and label.strip():
                labels.append(f"[{label.strip()}]")
        return labels

    @staticmethod
    def _blip_reaction_as_text(content: Any) -> str | None:
        """Reactions carry the emoji as Unicode code points; an empty list removes the reaction."""
        if not isinstance(content, dict) or not isinstance(content.get("emoji"), dict):
            return None
        values = content["emoji"].get("values")
        if not isinstance(values, list):
            return None
        emoji = "".join(
            chr(value)
            for value in values
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 0x10FFFF
            and not 0xD800 <= value <= 0xDFFF
        )
        text = f"[Reaction: {emoji}]" if emoji else "[Reaction removed]"
        target = content.get("inReactionTo")
        quoted = target.get("value") if isinstance(target, dict) else None
        if isinstance(quoted, str) and quoted.strip():
            quoted = quoted.strip()
            if len(quoted) > _REACTION_QUOTE_LIMIT:
                quoted = f"{quoted[:_REACTION_QUOTE_LIMIT].rstrip()}…"
            quote = "\n".join(f"> {line}" for line in quoted.splitlines())
            return f"{text}\n{quote}"
        return text

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
