"""add radioid user cache

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "radioid_users",
        sa.Column("dmr_id", sa.Integer(), nullable=False),
        sa.Column("callsign", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=96), nullable=True),
        sa.Column("city", sa.String(length=96), nullable=True),
        sa.Column("state", sa.String(length=96), nullable=True),
        sa.Column("country", sa.String(length=96), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("dmr_id"),
    )
    op.create_index("ix_radioid_users_callsign", "radioid_users", ["callsign"])


def downgrade() -> None:
    op.drop_index("ix_radioid_users_callsign", "radioid_users")
    op.drop_table("radioid_users")
