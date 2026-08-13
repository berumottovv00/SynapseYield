"""MarketHistoryTool：把 HistoryFetcher 包装成 OpenAI function-calling 工具，供闲聊节点按需查询历史行情。"""

from __future__ import annotations

import json

from synapse_yield.market.history import HistoryFetcher


class MarketHistoryTool:
    """接口约定见 HarnessAgentGraph._llm_chat：.name / .schema / .run_call(tool_call)。"""

    name = "get_market_history"

    def __init__(self, fetcher: HistoryFetcher) -> None:
        self._fetcher = fetcher

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "查询指定股票代码的历史 K 线行情，返回 markdown 表格摘要（含区间涨跌幅、最高最低价）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "股票代码，例如 AAPL.US、700.HK、600519.SH",
                        }
                    },
                    "required": ["symbol"],
                },
            },
        }

    def run_call(self, tool_call) -> str:
        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            return "参数解析失败：无法解析工具调用参数。"

        symbol = arguments.get("symbol")
        if not symbol:
            return "缺少必填参数 symbol。"

        try:
            return self._fetcher.fetch_markdown(symbol)
        except Exception as exc:
            return f"查询 {symbol} 历史行情失败：{exc}"
