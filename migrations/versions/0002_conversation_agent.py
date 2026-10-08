"""Remember which BLiP Desk agent a conversation was last assigned to.

Revision ID: 0002_conversation_agent
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_conversation_agent"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_mappings") as batch:
        batch.add_column(sa.Column("blip_agent_identity", sa.String(length=255)))


def downgrade() -> None:
    with op.batch_alter_table("conversation_mappings") as batch:
        batch.drop_column("blip_agent_identity")
