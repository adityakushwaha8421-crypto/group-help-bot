"""gateway choice (Betix / Native) on cases

Revision ID: c3d9a7e51b20
Revises: 00964d94889f
Create Date: 2026-11-01 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "c3d9a7e51b20"
down_revision = "00964d94889f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("gateway_choice", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("native_order_ids", sa.JSON(), nullable=True))
    # Every case that exists today went (or goes) the Betix way: it must not wait for an answer nobody is asked.
    op.execute("UPDATE cases SET gateway_choice = 'betix' WHERE kind = 'payment'")


def downgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_column("native_order_ids")
        batch_op.drop_column("gateway_choice")
