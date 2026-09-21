"""Create bridge persistence tables.

Revision ID: 0001_initial
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "conversation_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("blip_bot_identity", sa.String(length=255), nullable=False),
        sa.Column("blip_customer_identity", sa.String(length=512), nullable=False),
        sa.Column("chatwoot_account_id", sa.Integer(), nullable=False),
        sa.Column("chatwoot_contact_id", sa.Integer(), nullable=False),
        sa.Column("chatwoot_source_id", sa.String(length=512), nullable=False),
        sa.Column("chatwoot_conversation_id", sa.Integer(), nullable=False),
        sa.Column("blip_ticket_id", sa.String(length=255)),
        sa.Column("chatwoot_labels", JSON_TYPE, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("blip_tags", JSON_TYPE, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("blip_bot_identity", "blip_customer_identity", name="uq_blip_customer"),
        sa.UniqueConstraint(
            "chatwoot_account_id",
            "chatwoot_conversation_id",
            name="uq_chatwoot_conversation",
        ),
    )
    op.create_index("ix_mapping_ticket", "conversation_mappings", ["blip_ticket_id"])

    op.create_table(
        "inbound_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("delivery_id", sa.String(length=255)),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("result_id", sa.String(length=255)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "external_id", name="uq_inbound_provider_external"),
    )
    op.create_index("ix_inbound_status", "inbound_events", ["status", "created_at"])

    op.create_table(
        "outbox_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
    )
    op.create_index("ix_outbox_ready", "outbox_jobs", ["status", "next_attempt_at"])

    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_id", sa.Integer(), nullable=False),
        sa.Column("chatwoot_message_id", sa.Integer()),
        sa.Column("blip_message_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="sending"),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("chatwoot_message_id", name="uq_chatwoot_message_delivery"),
        sa.UniqueConstraint("blip_message_id", name="uq_blip_message_delivery"),
    )


def downgrade() -> None:
    op.drop_table("message_deliveries")
    op.drop_index("ix_outbox_ready", table_name="outbox_jobs")
    op.drop_table("outbox_jobs")
    op.drop_index("ix_inbound_status", table_name="inbound_events")
    op.drop_table("inbound_events")
    op.drop_index("ix_mapping_ticket", table_name="conversation_mappings")
    op.drop_table("conversation_mappings")
