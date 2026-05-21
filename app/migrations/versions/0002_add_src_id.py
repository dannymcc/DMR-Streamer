"""add src_id to listen_events

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listen_events", sa.Column("src_id", sa.Integer, nullable=True))
    op.create_index("ix_listen_events_src_id", "listen_events", ["src_id"])


def downgrade() -> None:
    op.drop_index("ix_listen_events_src_id", "listen_events")
    op.drop_column("listen_events", "src_id")
