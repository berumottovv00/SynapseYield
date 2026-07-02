"""市场数据工具：封装行情拉取，供 LLM 通过 function calling 按需调用。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from synapse_yield.market.history import HistoryFetcher

# OpenAI function calling schema
FETCH_HISTORY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "fetch_market_history",
        "description": (
            "Fetch historical daily K-line (OHLCV) data for a stock symbol as a markdown table. "
            "Call this to verify price trends, support/resistance levels, and recent volatility "
            "before finalizing your picks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock ticker with market suffix, e.g. AAPL.US or 00700.HK",
                }
            },
            "required": ["symbol"],
        },
    },
}


class MarketHistoryTool:
    """封装历史行情拉取，提供 OpenAI tool calling 所需的 schema 和执行入口。"""

    name = "fetch_market_history"
    schema = FETCH_HISTORY_SCHEMA

    def __init__(self, fetcher: HistoryFetcher) -> None:
        self._fetcher = fetcher

    def execute(self, symbol: str) -> str:
        return self._fetcher.fetch_markdown(symbol)

    def run_call(self, tool_call: Any) -> str:
        """解析 OpenAI tool_call 对象并执行，返回结果字符串。"""
        args = json.loads(tool_call.function.arguments)
        return self.execute(args["symbol"])
