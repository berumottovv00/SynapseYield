from decimal import Decimal

from sqlalchemy import select

from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.domain.enums import OrderSide, OrderType
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import MarketQuote, OrderIntent
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import Account, CashLedger, Fill, Order, Position, StrategySignal
from synapse_yield.storage.session import session_scope


def main() -> None:
    trace_id = new_id("trace")
    account_id = "demo_account"
    symbol = "AAPL.US"

    order_service = OrderService()
    broker = LocalSimBroker(order_service=order_service)

    # with 块内只要有任何异常抛出，数据库操作会自动回滚，不会留下脏数据。
    # with session_scope() as session: 块里的代码执行完、没有异常时，session_scope 自动调用 commit()
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            account = Account(
                account_id=account_id,
                base_currency="USD",
                cash_available=Decimal("100000"),
                cash_frozen=Decimal("0"),
                equity=Decimal("100000"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
            # 只放入内存，数据库完全不知道
            session.add(account)
            # 执行 SQL（INSERT/UPDATE），但事务未提交，可回滚
            session.flush()

        signal = StrategySignal(
            signal_id=new_id("sig"),
            trace_id=trace_id,
            strategy_name="demo_ma_breakout",
            strategy_version="v1",
            symbol=symbol,
            side=OrderSide.BUY,
            confidence=Decimal("0.800000"),
            target_quantity=Decimal("10"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("180.10"),
            reason="local_sim_smoke_test",
            raw_payload={"source": "demo"},
        )
        session.add(signal)
        session.flush()

        intent = OrderIntent(
            account_id=account_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            limit_price=Decimal("180.10"),
            source_signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
        )
        order = order_service.create_order_from_intent(session, trace_id, intent)
        risk_decision = order_service.run_risk_check(session, trace_id, order)

        if risk_decision.decision.value != "APPROVED":
            print(f"risk rejected: {risk_decision.reason_code} {risk_decision.message}")
            return

        quote = MarketQuote(
            symbol=symbol,
            last_price=Decimal("180.00"),
            bid_price=Decimal("179.95"),
            ask_price=Decimal("180.05"),
        )
        result = broker.submit_order(session, trace_id, order, quote)
        if not result.accepted:
            print(f"broker rejected: {result.message}")
            return

        session.flush()

        # 从数据库读取最终状态，用于打印验证结果，相当于一次"成交后对账快照"。
        order_snapshot = session.get(Order, order.order_id)
        position = session.scalar(
            select(Position).where(Position.account_id == account_id, Position.symbol == symbol)
        )
        fills = session.scalars(select(Fill).where(Fill.order_id == order.order_id)).all()
        ledger_entries = session.scalars(
            select(CashLedger).where(CashLedger.order_id == order.order_id)
        ).all()

        print("local sim trade completed")
        print(f"trace_id={trace_id}")
        print(f"order_id={order_snapshot.order_id}")
        print(f"order_status={order_snapshot.status.value}")
        print(f"filled_quantity={order_snapshot.filled_quantity}")
        print(f"avg_fill_price={order_snapshot.avg_fill_price}")
        print(f"cash_available={account.cash_available}")
        print(f"position_quantity={position.quantity if position else 0}")
        print(f"fills={len(fills)}")
        print(f"cash_ledger_entries={len(ledger_entries)}")


if __name__ == "__main__":
    main()

