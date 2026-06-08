"""策略适配、运行和历史行情回放。"""

from synapse_yield.strategy.adapters import CallableStrategyAdapter, StrategyAdapter
from synapse_yield.strategy.examples import PriceMoveStrategy
from synapse_yield.strategy.runner import StrategyRunner

__all__ = [
    "CallableStrategyAdapter",
    "PriceMoveStrategy",
    "StrategyAdapter",
    "StrategyRunner",
]
