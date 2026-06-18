"""StrategyAgent：纯 LLM 策略层，只负责分析报告并返回选股建议。

职责边界：
  - 调用 LLM Provider 分析市场报告，结合账户持仓给出 StockPick 列表。
  - 不持有 Broker、不做风控、不操作订单，与执行层完全解耦。
  - 由 HarnessAgentGraph 的 select 节点调用。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.agents.llm_provider import OpenAITradeProposalProvider, TradeProposalProvider
from synapse_yield.config import get_settings
from synapse_yield.domain.schemas import StockSelectionResult
from synapse_yield.market.history import HistoryFetcher
from synapse_yield.storage.models import Account, Position
from synapse_yield.storage.session import SessionLocal


class StrategyAgent:
    """无状态 LLM 策略 Agent，唯一的公开接口是 select_stocks()。"""

    def __init__(
        self,
        *,
        provider: TradeProposalProvider | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
        history_fetcher: HistoryFetcher | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.provider = provider or OpenAITradeProposalProvider()
        self.session_factory = session_factory
        self._history_fetcher = history_fetcher
        self.enabled = (
            get_settings().enable_llm_trading_agent if enabled is None else enabled
        )

    def select_stocks(
        self,
        market_content: str,
        account_id: str,
        n: int = 2,
        symbols: list[str] | None = None,
        instructions: str | None = None,
    ) -> StockSelectionResult:
        """分析调研报告，结合账户仓位挑选 n 支候选标的。

        symbols: 若提供，拉取长桥历史行情附加到报告末尾供 LLM 参考。
        instructions: 用户的调整指令（refine），累积追加，引导 LLM 重新筛选。
        """
        self._assert_enabled()

        if not market_content or not market_content.strip():
            return StockSelectionResult(
                picks=[],
                market_summary="报告内容为空，请先在左侧填写报告路径并加载文件。",
            )

        select_fn = getattr(self.provider, "select_stocks", None)
        if select_fn is None:
            raise RuntimeError("The configured provider does not support select_stocks()")

        content = market_content
        if self._history_fetcher is not None and symbols:
            history_sections = [
                self._history_fetcher.fetch_markdown(sym) for sym in symbols
            ]
            content = content + "\n\n---\n\n" + "\n\n".join(history_sections)

        account_context = self._load_account_context(account_id)
        return select_fn(content, account_context, n, instructions)

    def chat(self, message: str) -> str:
        """用 LLM 直接回答用户问题，不校验 enable 开关。"""
        client = getattr(self.provider, "client", None)
        model = getattr(self.provider, "model_name", "gpt-4o")
        if client is None:
            return "抱歉，当前 LLM 服务不可用。"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个专业的量化交易助手，熟悉股票市场、投资策略和风险管理。"
                        "请简洁、准确地回答用户问题。"
                    ),
                },
                {"role": "user", "content": message},
            ],
        )
        return response.choices[0].message.content or ""

    def classify_intent(self, text: str) -> str:
        """调用 LLM 将用户输入分类为 'select' / 'order' / 'chat'。"""
        client = getattr(self.provider, "client", None)
        model = getattr(self.provider, "model_name", "gpt-4o")
        if client is None:
            return "chat"
        response = client.chat.completions.create(
            model=model,
            max_tokens=5,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是交易系统的意图分类器。根据用户输入，只返回以下标签之一：\n"
                        "- select：用户想要选股、分析报告、或调整选股条件\n"
                        "- order：用户想要下单、买入或卖出股票\n"
                        "- chat：用户在闲聊或询问通用知识，不涉及具体操作\n"
                        "只返回标签本身，不要有其他文字。"
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        label = (response.choices[0].message.content or "").strip().lower()
        return label if label in ("select", "order", "chat") else "chat"

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _load_account_context(self, account_id: str) -> dict:
        session = self.session_factory()
        try:
            account = session.get(Account, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} does not exist")
            positions = session.scalars(
                select(Position).where(Position.account_id == account_id)
            ).all()
            return {
                "account": {
                    "account_id": account.account_id,
                    "base_currency": account.base_currency,
                    "cash_available": str(account.cash_available),
                    "cash_frozen": str(account.cash_frozen),
                    "equity": str(account.equity),
                    "realized_pnl": str(account.realized_pnl),
                    "unrealized_pnl": str(account.unrealized_pnl),
                },
                "positions": [
                    {
                        "symbol": p.symbol,
                        "quantity": str(p.quantity),
                        "available_quantity": str(p.available_quantity),
                        "avg_cost": str(p.avg_cost),
                        "market_price": str(p.market_price),
                        "market_value": str(p.market_value),
                        "unrealized_pnl": str(p.unrealized_pnl),
                    }
                    for p in positions
                ],
            }
        finally:
            session.close()

    def _assert_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                "LLM trading Agent is disabled. Set ENABLE_LLM_TRADING_AGENT=true."
            )
