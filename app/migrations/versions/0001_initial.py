"""initial

Revision ID: 0001
Revises:
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preferences",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(512)),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_table(
        "favourite_tgs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tg", sa.Integer, nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.Integer, default=0),
        sa.Column("added_at", sa.DateTime),
    )
    op.create_index("ix_favourite_tgs_tg", "favourite_tgs", ["tg"], unique=True)

    op.create_table(
        "listen_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tg", sa.Integer, nullable=False),
        sa.Column("callsign", sa.String(16)),
        sa.Column("name", sa.String(64)),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("heard_at", sa.DateTime),
    )
    op.create_index("ix_listen_events_tg", "listen_events", ["tg"])
    op.create_index("ix_listen_events_heard_at", "listen_events", ["heard_at"])

    op.execute(
        "INSERT INTO favourite_tgs (tg, label, sort_order, added_at) VALUES "
        "(2350, 'UK', 0, datetime('now')),"
        "(2351, 'Chat 1', 1, datetime('now')),"
        "(235, 'UK Call (legacy)', 2, datetime('now'))"
    )


def downgrade() -> None:
    op.drop_table("listen_events")
    op.drop_table("favourite_tgs")
    op.drop_table("preferences")
