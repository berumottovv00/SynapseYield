"""add bracket order types (LIMIT_IF_TOUCHED, STOP_LIMIT)

Revision ID: 0002_add_bracket_order_types
Revises: 0001_initial_schema
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_bracket_order_types"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_ORDER_TYPE_ENUM = "ENUM('MARKET','LIMIT','LIMIT_IF_TOUCHED','STOP_LIMIT')"
_OLD_ORDER_TYPE_ENUM = "ENUM('MARKET','LIMIT')"


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column(
            "order_type",
            existing_type=sa.Enum("MARKET", "LIMIT", name="ordertype"),
            type_=sa.Enum("MARKET", "LIMIT", "LIMIT_IF_TOUCHED", "STOP_LIMIT", name="ordertype"),
            existing_nullable=False,
        )
    with op.batch_alter_table("strategy_signals") as batch_op:
        batch_op.alter_column(
            "order_type",
            existing_type=sa.Enum("MARKET", "LIMIT", name="ordertype"),
            type_=sa.Enum("MARKET", "LIMIT", "LIMIT_IF_TOUCHED", "STOP_LIMIT", name="ordertype"),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column(
            "order_type",
            existing_type=sa.Enum("MARKET", "LIMIT", "LIMIT_IF_TOUCHED", "STOP_LIMIT", name="ordertype"),
            type_=sa.Enum("MARKET", "LIMIT", name="ordertype"),
            existing_nullable=False,
        )
    with op.batch_alter_table("strategy_signals") as batch_op:
        batch_op.alter_column(
            "order_type",
            existing_type=sa.Enum("MARKET", "LIMIT", "LIMIT_IF_TOUCHED", "STOP_LIMIT", name="ordertype"),
            type_=sa.Enum("MARKET", "LIMIT", name="ordertype"),
            existing_nullable=False,
        )
