from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


JsonType = JSON().with_variant(JSONB, "postgresql")


class ConversationMapping(Base):
    __tablename__ = "conversation_mappings"
    __table_args__ = (
        UniqueConstraint("blip_bot_identity", "blip_customer_identity", name="uq_blip_customer"),
        UniqueConstraint(
            "chatwoot_account_id",
            "chatwoot_conversation_id",
            name="uq_chatwoot_conversation",
        ),
        Index("ix_mapping_ticket", "blip_ticket_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blip_bot_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    blip_customer_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    chatwoot_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chatwoot_contact_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chatwoot_source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    chatwoot_conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    blip_ticket_id: Mapped[str | None] = mapped_column(String(255))
    chatwoot_labels: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    blip_tags: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_inbound_provider_external"),
        Index("ix_inbound_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    delivery_id: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    result_id: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
        Index("ix_outbox_ready", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MessageDelivery(Base):
    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("chatwoot_message_id", name="uq_chatwoot_message_delivery"),
        UniqueConstraint("blip_message_id", name="uq_blip_message_delivery"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chatwoot_message_id: Mapped[int | None] = mapped_column(Integer)
    blip_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="sending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
