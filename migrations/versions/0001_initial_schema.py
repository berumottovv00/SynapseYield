"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("base_currency", sa.String(length=8), nullable=False),
        sa.Column("cash_available", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash_frozen", sa.Numeric(20, 6), nullable=False),
        sa.Column("equity", sa.Numeric(20, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("account_id"),
        comment="账户资金表，记录账户现金、权益和盈亏汇总",
    )
    op.create_table(
        "market_snapshots",
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("open_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("high_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("low_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("volume", sa.Numeric(24, 6), nullable=True),
        sa.Column("turnover", sa.Numeric(24, 6), nullable=True),
        sa.Column("bid_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("ask_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id"),
        comment="行情快照表，记录策略计算和回放所需的标准化行情数据",
    )
    op.create_index("ix_market_snapshots_symbol", "market_snapshots", ["symbol"])
    op.create_index("ix_market_snapshots_trace_id", "market_snapshots", ["trace_id"])
    op.create_table(
        "strategy_signals",
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.Enum("BUY", "SELL", name="orderside"), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("target_quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("order_type", sa.Enum("MARKET", "LIMIT", name="ordertype"), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("signal_id"),
        comment="策略信号表，记录策略根据行情生成的买卖建议和原始输出",
    )
    op.create_index("ix_strategy_signals_symbol", "strategy_signals", ["symbol"])
    op.create_index("ix_strategy_signals_trace_id", "strategy_signals", ["trace_id"])
    op.create_table(
        "positions",
        sa.Column("position_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("available_quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("avg_cost", sa.Numeric(20, 6), nullable=False),
        sa.Column("market_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("market_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(20, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"]),
        sa.PrimaryKeyConstraint("position_id"),
        comment="持仓表，记录账户在每个证券上的当前持仓、成本和市值",
    )
    op.create_index("ux_positions_account_symbol", "positions", ["account_id", "symbol"], unique=True)
    op.create_table(
        "risk_decisions",
        sa.Column("risk_decision_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column(
            "decision",
            sa.Enum("APPROVED", "REJECTED", "REQUIRES_MANUAL_REVIEW", name="riskdecisionstatus"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=255), nullable=False),
        sa.Column("checked_rules", sa.JSON(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["strategy_signals.signal_id"]),
        sa.PrimaryKeyConstraint("risk_decision_id"),
        comment="风控决策表，记录每次订单意图的风控校验结果和原因",
    )
    op.create_index("ix_risk_decisions_trace_id", "risk_decisions", ["trace_id"])
    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.Enum("BUY", "SELL", name="orderside"), nullable=False),
        sa.Column("order_type", sa.Enum("MARKET", "LIMIT", name="ordertype"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "RISK_REJECTED",
                "RISK_APPROVED",
                "SUBMITTING",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCEL_PENDING",
                "CANCELLED",
                "FAILED",
                "RECONCILING",
                name="orderstatus",
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("time_in_force", sa.Enum("DAY", "GTC", name="timeinforce"), nullable=False),
        sa.Column("source_signal_id", sa.String(length=64), nullable=True),
        sa.Column("risk_decision_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("raw_request", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"]),
        sa.ForeignKeyConstraint(["risk_decision_id"], ["risk_decisions.risk_decision_id"]),
        sa.ForeignKeyConstraint(["source_signal_id"], ["strategy_signals.signal_id"]),
        sa.PrimaryKeyConstraint("order_id"),
        sa.UniqueConstraint("broker_order_id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("idempotency_key"),
        comment="订单主表，记录订单生命周期、状态机状态、幂等键和券商订单映射",
    )
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=64), nullable=False),
        sa.Column("actor_name", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_snapshot", sa.JSON(), nullable=False),
        sa.Column("previous_state", sa.String(length=64), nullable=True),
        sa.Column("next_state", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
        comment="审计日志表，记录 Harness 全链路动作、输入输出快照和状态迁移",
    )
    op.create_index("ix_audit_logs_trace_id", "audit_logs", ["trace_id"])
    op.create_table(
        "broker_events",
        sa.Column("broker_event_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("broker_name", sa.String(length=64), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("broker_fill_id", sa.String(length=128), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("broker_event_id"),
        comment="券商事件表，保存本地模拟盘或长桥推送的原始订单与成交事件",
    )
    op.create_index("ix_broker_events_broker_fill_id", "broker_events", ["broker_fill_id"])
    op.create_index("ix_broker_events_broker_order_id", "broker_events", ["broker_order_id"])
    op.create_index("ix_broker_events_trace_id", "broker_events", ["trace_id"])
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "PUBLISHED", "FAILED", name="outboxstatus"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        comment="本地消息表，用于可靠发布领域事件和实现最终一致性",
    )
    op.create_index("ix_outbox_events_trace_id", "outbox_events", ["trace_id"])
    op.create_table(
        "fills",
        sa.Column("fill_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("broker_fill_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.Enum("BUY", "SELL", name="orderside"), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("commission", sa.Numeric(20, 6), nullable=False),
        sa.Column("fill_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"]),
        sa.PrimaryKeyConstraint("fill_id"),
        sa.UniqueConstraint("broker_fill_id"),
        comment="成交明细表，记录订单的每笔成交、价格、数量和手续费",
    )
    op.create_index("ix_fills_symbol", "fills", ["symbol"])
    op.create_table(
        "cash_ledger",
        sa.Column("ledger_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("fill_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"]),
        sa.ForeignKeyConstraint(["fill_id"], ["fills.fill_id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"]),
        sa.PrimaryKeyConstraint("ledger_id"),
        comment="资金流水表，记录买入、卖出、手续费等导致的账户现金变化",
    )
    op.create_table(
        "reconcile_tasks",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
        comment="对账任务表，记录本地状态与券商状态不一致时的修复任务",
    )
    op.create_index("ix_reconcile_tasks_trace_id", "reconcile_tasks", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_reconcile_tasks_trace_id", table_name="reconcile_tasks")
    op.drop_table("reconcile_tasks")
    op.drop_table("cash_ledger")
    op.drop_index("ix_fills_symbol", table_name="fills")
    op.drop_table("fills")
    op.drop_index("ix_outbox_events_trace_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_broker_events_trace_id", table_name="broker_events")
    op.drop_index("ix_broker_events_broker_order_id", table_name="broker_events")
    op.drop_index("ix_broker_events_broker_fill_id", table_name="broker_events")
    op.drop_table("broker_events")
    op.drop_index("ix_audit_logs_trace_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_risk_decisions_trace_id", table_name="risk_decisions")
    op.drop_table("risk_decisions")
    op.drop_index("ux_positions_account_symbol", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_strategy_signals_trace_id", table_name="strategy_signals")
    op.drop_index("ix_strategy_signals_symbol", table_name="strategy_signals")
    op.drop_table("strategy_signals")
    op.drop_index("ix_market_snapshots_trace_id", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_symbol", table_name="market_snapshots")
    op.drop_table("market_snapshots")
    op.drop_table("accounts")
