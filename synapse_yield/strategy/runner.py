from collections.abc import Iterable

from sqlalchemy.orm import Session

from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import (
    MarketDataSnapshot,
    OrderIntent,
    ReplayResult,
    StrategyContext,
    StrategyOutput,
    StrategyRunResult,
)
from synapse_yield.storage.models import MarketSnapshot, StrategySignal
from synapse_yield.strategy.adapters import StrategyAdapter


class StrategyRunner:
    """执行策略、标准化结果并持久化行情和信号。"""

    def run(
        self,
        session: Session,
        trace_id: str,
        adapter: StrategyAdapter,
        snapshot: MarketDataSnapshot,
        context: StrategyContext,
        *,
        dry_run: bool = True,
        persist_snapshot: bool = True,
    ) -> StrategyRunResult:
        """运行一次策略；dry run 时记录信号但不生成可执行订单意图。"""

        snapshot_id = new_id("snapshot")
        if persist_snapshot:
            session.add(self._snapshot_model(snapshot_id, trace_id, snapshot))

        output = adapter.generate_signal(snapshot, context)
        if output is None:
            return StrategyRunResult(
                trace_id=trace_id,
                snapshot_id=snapshot_id,
                strategy_name=adapter.name,
                strategy_version=adapter.version,
                dry_run=dry_run,
            )

        signal_id = new_id("sig")
        session.add(
            self._signal_model(
                signal_id=signal_id,
                trace_id=trace_id,
                adapter=adapter,
                snapshot=snapshot,
                output=output,
                dry_run=dry_run,
            )
        )
        order_intent = None
        if not dry_run:
            # 第三阶段只产出订单意图，真正创建订单和提交 Broker 留给 Harness 编排。
            order_intent = OrderIntent(
                account_id=context.account_id,
                symbol=snapshot.symbol,
                side=output.side,
                order_type=output.order_type,
                quantity=output.target_quantity,
                limit_price=output.limit_price,
                source_signal_id=signal_id,
                strategy_name=adapter.name,
            )

        return StrategyRunResult(
            trace_id=trace_id,
            snapshot_id=snapshot_id,
            signal_id=signal_id,
            strategy_name=adapter.name,
            strategy_version=adapter.version,
            dry_run=dry_run,
            output=output,
            order_intent=order_intent,
        )

    def replay(
        self,
        session: Session,
        trace_id: str,
        adapter: StrategyAdapter,
        snapshots: Iterable[MarketDataSnapshot],
        *,
        account_id: str,
        parameters: dict | None = None,
        dry_run: bool = True,
    ) -> ReplayResult:
        """按行情时间顺序重放策略，并把此前快照作为下一次策略上下文。"""

        history: list[MarketDataSnapshot] = []
        runs: list[StrategyRunResult] = []
        ordered_snapshots = sorted(snapshots, key=lambda item: item.timestamp)
        for snapshot in ordered_snapshots:
            context = StrategyContext(
                account_id=account_id,
                previous_snapshots=tuple(history),
                parameters=parameters or {},
            )
            runs.append(
                self.run(
                    session,
                    trace_id,
                    adapter,
                    snapshot,
                    context,
                    dry_run=dry_run,
                )
            )
            history.append(snapshot)

        return ReplayResult(
            processed_snapshots=len(runs),
            generated_signals=sum(run.signal_id is not None for run in runs),
            runs=tuple(runs),
        )

    @staticmethod
    def _snapshot_model(
        snapshot_id: str,
        trace_id: str,
        snapshot: MarketDataSnapshot,
    ) -> MarketSnapshot:
        return MarketSnapshot(
            snapshot_id=snapshot_id,
            trace_id=trace_id,
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            last_price=snapshot.last_price,
            open_price=snapshot.open_price,
            high_price=snapshot.high_price,
            low_price=snapshot.low_price,
            volume=snapshot.volume,
            turnover=snapshot.turnover,
            bid_price=snapshot.bid_price,
            ask_price=snapshot.ask_price,
            source=snapshot.source,
        )

    @staticmethod
    def _signal_model(
        signal_id: str,
        trace_id: str,
        adapter: StrategyAdapter,
        snapshot: MarketDataSnapshot,
        output: StrategyOutput,
        dry_run: bool,
    ) -> StrategySignal:
        raw_payload = {
            **output.raw_payload,
            "dry_run": dry_run,
            "snapshot_timestamp": snapshot.timestamp.isoformat(),
            "snapshot_source": snapshot.source,
        }
        return StrategySignal(
            signal_id=signal_id,
            trace_id=trace_id,
            strategy_name=adapter.name,
            strategy_version=adapter.version,
            symbol=snapshot.symbol,
            side=output.side,
            confidence=output.confidence,
            target_quantity=output.target_quantity,
            order_type=output.order_type,
            limit_price=output.limit_price,
            reason=output.reason,
            raw_payload=raw_payload,
        )
