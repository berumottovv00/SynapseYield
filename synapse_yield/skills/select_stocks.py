"""SelectStocksSkill：选股技能，由 HarnessAgent 的 select 节点调用。

职责：
  - 加载账户上下文（持仓、资金）
  - 调用 LLM Provider 分析报告，按需通过 tools 拉取行情
  - 返回结构化选股结果
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.agents.llm_provider import OpenAITradeProposalProvider, TradeProposalProvider
from synapse_yield.config import get_settings
from synapse_yield.domain.schemas import StockSelectionResult
from synapse_yield.storage.models import Account, Position
from synapse_yield.storage.session import SessionLocal


class SelectStocksSkill:
    """选股技能：无状态，唯一对外接口是 __call__()。"""

    def __init__(
        self,
        *,
        provider: TradeProposalProvider | None = None,
        tools: list | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
        enabled: bool | None = None,
    ) -> None:
        self.provider = provider or OpenAITradeProposalProvider()
        self._tools = tools or []
        self._session_factory = session_factory
        self.enabled = (
            get_settings().enable_llm_trading_agent if enabled is None else enabled
        )

    def __call__(
        self,
        market_content: str,
        account_id: str,
        n: int = 2,
        instructions: str | None = None,
        custom_prompt: str | None = None,
    ) -> StockSelectionResult:
        if not self.enabled:
            raise RuntimeError(
                "LLM trading Agent is disabled. Set ENABLE_LLM_TRADING_AGENT=true."
            )
        if not market_content or not market_content.strip():
            return StockSelectionResult(
                picks=[],
                market_summary="报告内容为空，请先加载报告文件。",
            )

        select_fn = getattr(self.provider, "select_stocks", None)
        if select_fn is None:
            raise RuntimeError("The configured provider does not support select_stocks()")

        account_context = self._load_account_context(account_id)
        return select_fn(
            market_content,
            account_context,
            n,
            instructions,
            custom_prompt,
            self._tools or None,
        )

    def _load_account_context(self, account_id: str) -> dict:
        session = self._session_factory()
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
