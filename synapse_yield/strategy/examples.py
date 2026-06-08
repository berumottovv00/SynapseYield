from decimal import Decimal

from synapse_yield.domain.enums import OrderSide, OrderType
from synapse_yield.domain.schemas import MarketDataSnapshot, StrategyContext, StrategyOutput


class PriceMoveStrategy:
    """根据相邻两次价格变化生成信号的确定性示例策略。"""

    name = "price_move"
    version = "v1"

    def generate_signal(
        self,
        snapshot: MarketDataSnapshot,
        context: StrategyContext,
    ) -> StrategyOutput | None:
        threshold = Decimal(str(context.parameters.get("threshold", "0.01")))
        quantity = Decimal(str(context.parameters.get("quantity", "1")))
        if not context.previous_snapshots:
            return None

        previous = context.previous_snapshots[-1]
        if previous.symbol != snapshot.symbol:
            return None

        change_ratio = (snapshot.last_price - previous.last_price) / previous.last_price
        if abs(change_ratio) < threshold:
            return None

        side = OrderSide.BUY if change_ratio > 0 else OrderSide.SELL
        confidence = min(abs(change_ratio) / threshold, Decimal("1"))
        return StrategyOutput(
            side=side,
            confidence=confidence,
            target_quantity=quantity,
            order_type=OrderType.MARKET,
            reason="price_move_threshold_crossed",
            raw_payload={
                "previous_price": str(previous.last_price),
                "current_price": str(snapshot.last_price),
                "change_ratio": str(change_ratio),
                "threshold": str(threshold),
            },
        )
