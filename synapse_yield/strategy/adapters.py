from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from synapse_yield.domain.schemas import MarketDataSnapshot, StrategyContext, StrategyOutput


@runtime_checkable
class StrategyAdapter(Protocol):
    """所有策略适配器都必须满足的稳定接口。"""

    name: str
    version: str

    def generate_signal(
        self,
        snapshot: MarketDataSnapshot,
        context: StrategyContext,
    ) -> StrategyOutput | None:
        """根据标准行情和上下文生成信号；None 表示本次不交易。"""


class CallableStrategyAdapter:
    """把现有 Python 策略函数包装成标准 StrategyAdapter。"""

    def __init__(
        self,
        name: str,
        version: str,
        strategy: Callable[[MarketDataSnapshot, StrategyContext], StrategyOutput | dict | None],
    ):
        self.name = name
        self.version = version
        self._strategy = strategy

    def generate_signal(
        self,
        snapshot: MarketDataSnapshot,
        context: StrategyContext,
    ) -> StrategyOutput | None:
        # 允许旧策略返回字典，由 Pydantic 在 Adapter 边界完成校验和标准化。
        raw_output: Any = self._strategy(snapshot, context)
        if raw_output is None:
            return None
        return StrategyOutput.model_validate(raw_output)
