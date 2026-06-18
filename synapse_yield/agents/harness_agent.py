"""Harness Agent：顶层 LangGraph 图，协调策略选股 → 人工审批 → Broker 执行。

职责划分：
  - strategy_agent: select_stocks()（LLM 分析报告）；chat()（闲聊）；classify_intent()（意图分类）
  - executor:       风控 + Broker 提交 + 止盈/止损子单（与 LLM 完全解耦）
  - HarnessAgent:   LangGraph 图，串联上述两者，interrupt() 是唯一的人机交互点

图结构：
    START → interpret ──select──► select → wait_approval → execute → END
                      ──order──► execute → END
                      ──chat───► chat_reply → END
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, NotRequired, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

if TYPE_CHECKING:
    from synapse_yield.agents.strategy_agent import StrategyAgent
    from synapse_yield.harness.trade_executor import TradeExecutor

# push_fn(**kwargs) — 由 app.py 注入，线程安全地推送消息到 WebSocket
PushFn = Callable[..., None]

_ORDER_KEYWORDS = ("下单", "买入", "卖出", "执行下单", "提交订单")
_SELECT_KEYWORDS = ("选股", "帮我选", "挑几只", "推荐股")


class HarnessState(TypedDict):
    user_text: str            # 用户原始输入，由 interpret 节点解析
    account_id: str
    reviewer: str
    report_content: str
    n: int
    symbols: list[str] | None
    instructions: str | None  # 累积的 refine 指令，select 意图时维护
    intent: NotRequired[str]              # interpret 节点输出：select / order / chat
    pending_picks: NotRequired[list[dict]]  # 上次选股结果（由 app.py 传入）
    picks: NotRequired[list[dict]]
    confirmed_picks: NotRequired[list[dict]]


class HarnessAgentGraph:

    def __init__(
        self,
        *,
        strategy_agent: StrategyAgent,
        executor: TradeExecutor,
        push_fn: PushFn,
        checkpointer=None,
    ) -> None:
        self._strategy = strategy_agent
        self._executor = executor
        self._push = push_fn
        self._compiled_graph = self._build(checkpointer or MemorySaver())

    # ── 构建图 ────────────────────────────────────────────────────────────────

    def _build(self, checkpointer):
        graph = StateGraph(HarnessState)

        graph.add_node("interpret", self._interpret)
        graph.add_node("select", self._select)
        graph.add_node("wait_approval", self._wait_approval)
        graph.add_node("execute", self._execute)
        graph.add_node("chat_reply", self._chat)

        graph.add_edge(START, "interpret")
        graph.add_conditional_edges(
            "interpret",
            # 读取 _interpret 节点写入的 intent 字段，默认值chat
            lambda s: s.get("intent", "chat"),
            {"select": "select", "order": "execute", "chat": "chat_reply"},
        )
        graph.add_conditional_edges(
            "select",
            lambda s: "wait_approval" if s.get("picks") else END,
            {"wait_approval": "wait_approval", END: END},
        )
        graph.add_conditional_edges(
            "wait_approval",
            lambda s: "execute" if s.get("confirmed_picks") else END,
            {"execute": "execute", END: END},
        )
        graph.add_edge("execute", END)
        graph.add_edge("chat_reply", END)

        return graph.compile(checkpointer=checkpointer)

    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def run(self, thread_id: str, state: dict) -> dict:
        """开始一次新的图运行；在 interrupt（等待审批）或完成时返回最终状态。"""
        config = {"configurable": {"thread_id": thread_id}}
        return self._compiled_graph.invoke(state, config)

    def resume(self, thread_id: str, confirmed_picks: list[dict]) -> None:
        """以用户确认的标的列表恢复已暂停的图。"""
        from langgraph.types import Command
        # Command是那个唤醒信号
        config = {"configurable": {"thread_id": thread_id}}
        self._compiled_graph.invoke(
            Command(resume={"confirmed_picks": confirmed_picks}),
            config,
        )

    # ── 节点实现 ──────────────────────────────────────────────────────────────

    def _interpret(self, state: HarnessState) -> dict:
        """分类用户意图：select / order / chat。

        - 关键词快速识别 order（下单/买入/卖出）和显式 select（选股）
        - 其余情况调用 LLM 分类（区分 refine-select 与 chat）
        """
        text = state.get("user_text", "").strip()

        # order 意图：关键词触发，直接用 pending_picks 执行
        if any(kw in text for kw in _ORDER_KEYWORDS):
            pending = list(state.get("pending_picks") or [])
            return {"intent": "order", "confirmed_picks": pending}

        # 显式 select：新一轮选股，清空累积指令
        if re.match(r"^选股", text) or any(kw in text for kw in _SELECT_KEYWORDS):
            return {"intent": "select", "instructions": None}

        # 模糊情况：LLM 分类
        intent = self._strategy.classify_intent(text)

        if intent == "order":
            pending = list(state.get("pending_picks") or [])
            return {"intent": "order", "confirmed_picks": pending}

        if intent == "select":
            # refine：追加到已有累积指令
            prev = state.get("instructions") or ""
            new_instructions = (prev + "；" + text).strip("；") if prev else text
            return {"intent": "select", "instructions": new_instructions}

        # chat 意图（或 fallback）
        return {"intent": "chat"}

    def _select(self, state: HarnessState) -> dict:
        self._push(type="thinking", text=f"正在分析报告，挑选 {state['n']} 支股票……")

        result = self._strategy.select_stocks(
            state["report_content"],
            state["account_id"],
            n=state["n"],
            symbols=state.get("symbols"),
            instructions=state.get("instructions"),
        )

        if result.market_summary:
            self._push(type="summary", text=result.market_summary)

        picks = [p.model_dump(mode="json") for p in result.picks] if result.picks else []

        if picks:
            self._push(
                type="picks",
                picks=picks,
                account_id=state["account_id"],
                reviewer=state["reviewer"],
            )
        else:
            self._push(type="message", text="未获得选股建议，请调整指令后重试。")

        return {"picks": picks}

    @staticmethod
    def _wait_approval(state: HarnessState) -> dict:
        from langgraph.types import interrupt

        approval = interrupt({"type": "PICKS_APPROVAL_REQUIRED"})
        confirmed = (
            approval.get("confirmed_picks", []) if isinstance(approval, dict) else []
        )
        return {"confirmed_picks": confirmed}

    def _execute(self, state: HarnessState) -> dict:
        from synapse_yield.domain.enums import OrderType
        from synapse_yield.domain.schemas import (
            HumanApprovalDecision,
            LLMTradeProposal,
            StockPick,
        )

        confirmed = state.get("confirmed_picks") or []
        if not confirmed:
            self._push(type="message", text="暂无待下单的股票，请先输入「选股」获取推荐标的。")
            return {}

        self._push(type="thinking", text=f"正在提交 {len(confirmed)} 笔订单……")

        for raw in confirmed:
            try:
                pick = StockPick.model_validate(raw)
                proposal = LLMTradeProposal(
                    action=pick.action,
                    symbol=pick.symbol,
                    confidence=pick.confidence,
                    quantity=pick.quantity,
                    order_type=OrderType.LIMIT if pick.suggested_limit_price else OrderType.MARKET,
                    limit_price=pick.suggested_limit_price,
                    take_profit_price=pick.take_profit_price,
                    stop_loss_price=pick.stop_loss_price,
                    rationale=pick.rationale,
                    risk_notes=pick.risk_notes,
                )
                decision = HumanApprovalDecision(approved=True, reviewer=state["reviewer"])

                price = str(pick.suggested_limit_price) if pick.suggested_limit_price else "市价"
                self._push(
                    type="executing",
                    text=f"提交 {pick.symbol} {pick.action.value} {pick.quantity} 股 @ {price}……",
                )
                result = self._executor.execute(
                    proposal,
                    decision,
                    account_id=state["account_id"],
                )
                self._push(type="order_result", data=result.model_dump(mode="json"))
            except Exception as exc:
                self._push(type="error", text=f"{raw.get('symbol', '?')} 下单失败：{exc}")

        self._push(type="done", text="全部处理完毕。")
        return {}

    def _chat(self, state: HarnessState) -> dict:
        text = state.get("user_text", "")
        response = self._strategy.chat(text)
        self._push(type="message", text=response)
        return {}
