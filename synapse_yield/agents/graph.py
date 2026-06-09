from dataclasses import dataclass
from typing import NotRequired, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse_yield.broker.local_sim import LocalSimBroker
from synapse_yield.domain.enums import OrderStatus
from synapse_yield.domain.ids import new_id
from synapse_yield.domain.schemas import (
    HarnessRunResult,
    MarketDataSnapshot,
    MarketQuote,
    StrategyContext,
    StrategyRunResult,
)
from synapse_yield.harness.order_service import OrderService
from synapse_yield.storage.models import Order, StrategySignal
from synapse_yield.strategy.adapters import StrategyAdapter
from synapse_yield.strategy.runner import StrategyRunner


class AgentGraphState(TypedDict):
    """LangGraph 在四个 Agent 节点之间传递的交易状态。"""

    session: Session
    adapter: StrategyAdapter
    snapshot: MarketDataSnapshot
    account_id: str
    trace_id: str
    dry_run: bool
    strategy_parameters: dict
    market_is_open: bool | None
    visited_agents: list[str]
    quote: NotRequired[MarketQuote]
    strategy_result: NotRequired[StrategyRunResult]
    order: NotRequired[Order]
    risk_decision_id: NotRequired[str]
    broker_order_id: NotRequired[str | None]
    result: NotRequired[HarnessRunResult]


@dataclass(frozen=True)
class AgentGraphConfig:
    """构建 Agent 图所需的确定性服务。"""

    strategy_runner: StrategyRunner
    order_service: OrderService
    broker: LocalSimBroker


class MarketAgent:
    """Market Agent：接收并标准化行情事件。"""

    def __call__(self, state: AgentGraphState) -> AgentGraphState:
        snapshot = state["snapshot"]
        state["visited_agents"].append("market")
        state["quote"] = MarketQuote(
            symbol=snapshot.symbol,
            last_price=snapshot.last_price,
            bid_price=snapshot.bid_price,
            ask_price=snapshot.ask_price,
        )
        return state


class StrategyAgent:
    """Strategy Agent：调用策略适配器生成标准信号或订单意图。"""

    def __init__(self, strategy_runner: StrategyRunner):
        self.strategy_runner = strategy_runner

    def __call__(self, state: AgentGraphState) -> AgentGraphState:
        state["visited_agents"].append("strategy")
        if not state["dry_run"]:
            existing_order = self._find_existing_order(state)
            if existing_order is not None:
                state["order"] = existing_order
                state["result"] = self._existing_order_result(state, existing_order)
                return state

        context = StrategyContext(
            account_id=state["account_id"],
            parameters=state["strategy_parameters"],
        )
        strategy_result = self.strategy_runner.run(
            session=state["session"],
            trace_id=state["trace_id"],
            adapter=state["adapter"],
            snapshot=state["snapshot"],
            context=context,
            dry_run=state["dry_run"],
        )
        state["strategy_result"] = strategy_result

        if strategy_result.output is None:
            state["result"] = HarnessRunResult(
                trace_id=state["trace_id"],
                snapshot_id=strategy_result.snapshot_id,
                agent_path=tuple(state["visited_agents"]),
                dry_run=state["dry_run"],
                message="Strategy produced no signal",
            )
        elif state["dry_run"]:
            state["result"] = HarnessRunResult(
                trace_id=state["trace_id"],
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                agent_path=tuple(state["visited_agents"]),
                dry_run=True,
                message="Dry run completed without order creation",
            )
        return state

    @staticmethod
    def _find_existing_order(state: AgentGraphState) -> Order | None:
        return state["session"].scalar(
            select(Order)
            .join(StrategySignal, Order.source_signal_id == StrategySignal.signal_id)
            .where(
                StrategySignal.trace_id == state["trace_id"],
                StrategySignal.strategy_name == state["adapter"].name,
                StrategySignal.symbol == state["snapshot"].symbol,
            )
        )

    @staticmethod
    def _existing_order_result(state: AgentGraphState, order: Order) -> HarnessRunResult:
        return HarnessRunResult(
            trace_id=state["trace_id"],
            signal_id=order.source_signal_id,
            order_id=order.order_id,
            risk_decision_id=order.risk_decision_id,
            broker_order_id=order.broker_order_id,
            order_status=order.status,
            agent_path=tuple(state["visited_agents"]),
            dry_run=False,
            message="Idempotency hit: existing order reused",
        )


class RiskAgent:
    """Risk Agent：创建订单并执行确定性风控。"""

    def __init__(self, order_service: OrderService):
        self.order_service = order_service

    def __call__(self, state: AgentGraphState) -> AgentGraphState:
        state["visited_agents"].append("risk")
        strategy_result = state.get("strategy_result")
        if strategy_result is None or strategy_result.order_intent is None:
            return state

        order = self.order_service.create_order_from_intent(
            state["session"],
            state["trace_id"],
            strategy_result.order_intent,
        )
        state["order"] = order
        if order.status != OrderStatus.CREATED:
            state["result"] = HarnessRunResult(
                trace_id=state["trace_id"],
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                order_id=order.order_id,
                risk_decision_id=order.risk_decision_id,
                broker_order_id=order.broker_order_id,
                order_status=order.status,
                agent_path=tuple(state["visited_agents"]),
                dry_run=False,
                message="Idempotency hit: existing order reused",
            )
            return state

        risk_decision = self.order_service.run_risk_check(
            state["session"],
            state["trace_id"],
            order,
            quote=state["quote"],
            market_is_open=state["market_is_open"],
        )
        state["risk_decision_id"] = risk_decision.risk_decision_id
        if order.status == OrderStatus.RISK_REJECTED:
            state["result"] = HarnessRunResult(
                trace_id=state["trace_id"],
                snapshot_id=strategy_result.snapshot_id,
                signal_id=strategy_result.signal_id,
                order_id=order.order_id,
                risk_decision_id=risk_decision.risk_decision_id,
                order_status=order.status,
                agent_path=tuple(state["visited_agents"]),
                dry_run=False,
                message=f"Risk rejected order: {risk_decision.reason_code}",
            )
        return state


class ExecutionAgent:
    """Execution Agent：只提交已经通过风控的订单。"""

    def __init__(self, broker: LocalSimBroker):
        self.broker = broker

    def __call__(self, state: AgentGraphState) -> AgentGraphState:
        state["visited_agents"].append("execution")
        order = state.get("order")
        strategy_result = state.get("strategy_result")
        if order is None or strategy_result is None:
            return state

        broker_result = self.broker.submit_order(
            state["session"],
            state["trace_id"],
            order,
            state["quote"],
        )
        state["broker_order_id"] = broker_result.broker_order_id
        state["result"] = HarnessRunResult(
            trace_id=state["trace_id"],
            snapshot_id=strategy_result.snapshot_id,
            signal_id=strategy_result.signal_id,
            order_id=order.order_id,
            risk_decision_id=state.get("risk_decision_id"),
            broker_order_id=broker_result.broker_order_id,
            order_status=order.status,
            agent_path=tuple(state["visited_agents"]),
            dry_run=False,
            message=broker_result.message,
        )
        return state


class TradingAgentGraph:
    """由 LangGraph 管理的四 Agent 交易编排。"""

    def __init__(self, config: AgentGraphConfig | None = None):
        order_service = config.order_service if config else OrderService()
        self.config = config or AgentGraphConfig(
            strategy_runner=StrategyRunner(),
            order_service=order_service,
            broker=LocalSimBroker(order_service=order_service),
        )
        self.market_agent = MarketAgent()
        self.strategy_agent = StrategyAgent(self.config.strategy_runner)
        self.risk_agent = RiskAgent(self.config.order_service)
        self.execution_agent = ExecutionAgent(self.config.broker)
        self._compiled_graph = None

    def run_market_snapshot(
        self,
        session: Session,
        adapter: StrategyAdapter,
        snapshot: MarketDataSnapshot,
        *,
        account_id: str,
        trace_id: str | None = None,
        strategy_parameters: dict | None = None,
        dry_run: bool = False,
        market_is_open: bool | None = None,
    ) -> HarnessRunResult:
        """通过 LangGraph 四 Agent 处理单条行情。"""

        initial_state: AgentGraphState = {
            "session": session,
            "adapter": adapter,
            "snapshot": snapshot,
            "account_id": account_id,
            "trace_id": trace_id or new_id("trace"),
            "dry_run": dry_run,
            "strategy_parameters": strategy_parameters or {},
            "market_is_open": market_is_open,
            "visited_agents": [],
        }
        final_state = self._graph().invoke(initial_state)
        return final_state["result"]

    def _graph(self):
        """懒加载 LangGraph，未安装依赖时给出明确错误。"""

        if self._compiled_graph is not None:
            return self._compiled_graph

        try:
            from langgraph.graph import END, START, StateGraph
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "LangGraph is required for TradingAgentGraph. "
                'Install dependencies with `pip install -e ".[dev]"`.'
            ) from exc

        graph = StateGraph(AgentGraphState)
        graph.add_node("market_agent", self.market_agent)
        graph.add_node("strategy_agent", self.strategy_agent)
        graph.add_node("risk_agent", self.risk_agent)
        graph.add_node("execution_agent", self.execution_agent)
        graph.add_edge(START, "market_agent")
        graph.add_edge("market_agent", "strategy_agent")
        graph.add_conditional_edges(
            "strategy_agent",
            self._route_after_strategy,
            {
                "risk": "risk_agent",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "risk_agent",
            self._route_after_risk,
            {
                "execution": "execution_agent",
                "end": END,
            },
        )
        graph.add_edge("execution_agent", END)
        self._compiled_graph = graph.compile()
        return self._compiled_graph

    @staticmethod
    def _route_after_strategy(state: AgentGraphState) -> str:
        if "result" in state:
            return "end"
        return "risk"

    @staticmethod
    def _route_after_risk(state: AgentGraphState) -> str:
        if "result" in state:
            return "end"
        return "execution"
