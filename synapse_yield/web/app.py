"""FastAPI WebSocket 服务：连接聊天前端与 Harness Agent。"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from synapse_yield.orchestrator.harness_agent import HarnessAgentGraph
from synapse_yield.orchestrator.llm_provider import DEFAULT_SYSTEM_PROMPT
from synapse_yield.skills.select_stocks import SelectStocksSkill
from synapse_yield.broker.factory import build_broker
from synapse_yield.config import get_settings
from synapse_yield.harness.order_service import OrderService

app = FastAPI(title="SynapseYield Chat")

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

_REPORT_DIR = Path("/Users/shiyu/PyCharmMiscProject/project-Deep_Research/results")

# LLM / Broker 调用阻塞，跑在线程池里
_executor = ThreadPoolExecutor(max_workers=4)


# ── 全局单例（启动时初始化一次，所有会话共享）────────────────────────────────────

def _build_shared_services():
    from synapse_yield.harness.trade_executor import TradeExecutor

    order_service = OrderService()
    broker = build_broker(order_service=order_service)

    tools = []
    try:
        from synapse_yield.market.history import LongbridgeHistoryFetcher
        from synapse_yield.tools.market import MarketHistoryTool
        tools = [MarketHistoryTool(LongbridgeHistoryFetcher())]
    except Exception:
        pass

    select_skill = SelectStocksSkill(tools=tools)
    executor = TradeExecutor(order_service=order_service, broker=broker, strategy_name="harness_agent")
    return select_skill, executor, tools


_shared_select_skill: SelectStocksSkill | None = None
_shared_executor = None
_shared_tools: list = []


@app.on_event("startup")
async def _startup():
    global _shared_select_skill, _shared_executor, _shared_tools
    _shared_select_skill, _shared_executor, _shared_tools = _build_shared_services()


# ── 会话状态 ──────────────────────────────────────────────────────────────────

class SessionState:
    """单个 WebSocket 连接的持久上下文。"""

    def __init__(self) -> None:
        self.account_id: str = ""
        self.reviewer: str = "user"  # 硬编码，审批人即当前用户
        self.report_content: str = ""
        self.report_path: str = ""
        self.n: int = 2
        self.custom_prompt: str | None = None
        self.instructions: str | None = None   # 累积的 refine 指令
        self.last_picks: list[dict] = []       # 最近一次选股结果（供「下单」意图使用）
        self.thread_id: str | None = None      # 当前 LangGraph checkpoint 线程 ID
        self.agent: HarnessAgentGraph | None = None  # 每个连接独立的 agent 实例


# ── 连接管理 ──────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._sessions: dict[str, SessionState] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        # WebSocket 连接建立时，客户端发起握手请求，服务端必须显式调用 ws.accept() 来接受这个连接，握手才算完成。
        await ws.accept()
        self._connections[session_id] = ws
        self._sessions[session_id] = SessionState()

    def disconnect(self, session_id: str) -> None:
        self._connections.pop(session_id, None)
        self._sessions.pop(session_id, None)

    async def push(self, session_id: str, payload: dict) -> None:
        ws = self._connections.get(session_id)
        if ws:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    def state(self, session_id: str) -> SessionState:
        return self._sessions.setdefault(session_id, SessionState())


_manager = ConnectionManager()


# ── Agent 工厂 ────────────────────────────────────────────────────────────────

def _build_harness_agent(
    session_id: str,
    loop: asyncio.AbstractEventLoop,
) -> HarnessAgentGraph:
    """为会话创建 HarnessAgentGraph，只绑定 push_fn；strategy/executor 复用全局单例。"""
    def push_fn(**kwargs: object) -> None:
        # push_fn 是在工作线程（线程池）里被调用的，但 WebSocket 发送只能在**事件循环（主线程）**里做。直接在工作线程里 await 会报错。
        # run_coroutine_threadsafe 就是专门解决这个问题的：从线程池里把任务"投递"给事件循环去执行。
        asyncio.run_coroutine_threadsafe(_manager.push(session_id, kwargs), loop)

    return HarnessAgentGraph(
        select_skill=_shared_select_skill,
        executor=_shared_executor,
        tools=_shared_tools,
        push_fn=push_fn,
    )


# ── 路由 ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/prompt")
async def get_prompt() -> JSONResponse:
    return JSONResponse({"prompt": DEFAULT_SYSTEM_PROMPT})


@app.get("/api/report")
async def get_report() -> JSONResponse:
    today = datetime.date.today().strftime("%Y-%m-%d")
    path = _REPORT_DIR / f"output_report_{today}.md"
    if path.exists():
        return JSONResponse({"found": True, "content": path.read_text(encoding="utf-8"), "path": str(path)})
    return JSONResponse({"found": False, "content": "", "path": str(path)})


@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str) -> None:
    await _manager.connect(session_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 把 _dispatch(session_id, msg) 这个协程扔给事件循环异步执行，不等它完成就继续往下走。
            asyncio.create_task(_dispatch(session_id, msg))
    except WebSocketDisconnect:
        _manager.disconnect(session_id)


# ── 消息分发 ──────────────────────────────────────────────────────────────────

async def _dispatch(session_id: str, msg: dict) -> None:
    match msg.get("type"):
        case "message":
            await _handle_message(session_id, msg)
        case "approve":
            await _handle_approve(session_id, msg)
        case _:
            await _push(session_id, type="error", text=f"未知消息类型：{msg.get('type')}")


async def _push(session_id: str, **kwargs: object) -> None:
    await _manager.push(session_id, kwargs)


# ── 用户消息处理 ──────────────────────────────────────────────────────────────
# app.py 只负责：更新会话参数、读取报告文件、构建 Agent。
# 意图识别（选股/refine/报错）由 HarnessAgent 的 interpret 节点负责。

async def _handle_message(session_id: str, msg: dict) -> None:
    text: str = msg.get("text", "").strip()
    if not text:
        return

    state = _manager.state(session_id)

    # 更新侧栏参数
    account_id: str = msg.get("account_id", "").strip() or state.account_id
    n: int = int(msg.get("n") or state.n)
    custom_prompt: str | None = msg.get("custom_prompt") or state.custom_prompt

    state.account_id = account_id
    state.n = n
    state.custom_prompt = custom_prompt

    # 自动加载当日报告（首次或日期更新时重新读取）
    today = datetime.date.today().strftime("%Y-%m-%d")
    auto_path = _REPORT_DIR / f"output_report_{today}.md"
    if str(auto_path) != state.report_path:
        if auto_path.exists():
            try:
                state.report_content = auto_path.read_text(encoding="utf-8")
                state.report_path = str(auto_path)
            except Exception as exc:
                await _push(session_id, type="error", text=f"读取报告失败：{exc}")

    if not state.account_id:
        await _push(session_id, type="error", text="请在左侧填写账户 ID")
        return

    thread_id = str(uuid.uuid4())
    state.thread_id = thread_id

    loop = asyncio.get_running_loop()
    if state.agent is None:
        state.agent = _build_harness_agent(session_id, loop)

    harness_state = {
        "user_text": text,
        "account_id": state.account_id,
        "reviewer": state.reviewer,
        "report_content": state.report_content,
        "n": state.n,
        "instructions": state.instructions,
        "custom_prompt": state.custom_prompt,
        "pending_picks": state.last_picks,
    }

    try:
        result = await loop.run_in_executor(
            _executor,
            lambda: state.agent.run(thread_id, harness_state),
        )
        if isinstance(result, dict):
            # 同步 instructions（refine 累积）
            state.instructions = result.get("instructions")
            # 同步 picks：select 意图产出后等待用户确认，下一轮「下单」可复用
            picks = result.get("picks")
            if picks:
                state.last_picks = picks
    except Exception as exc:
        await _push(session_id, type="error", text=f"Agent 执行失败：{exc}")


# ── 用户审批处理（确认下单）──────────────────────────────────────────────────────

async def _handle_approve(session_id: str, msg: dict) -> None:
    confirmed_picks: list[dict] = msg.get("picks", [])
    state = _manager.state(session_id)

    if not state.thread_id or state.agent is None:
        await _push(session_id, type="error", text="没有待审批的选股，请先输入「选股」。")
        return

    thread_id = state.thread_id
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(
            _executor,
            lambda: state.agent.resume(thread_id, confirmed_picks),
        )
    except Exception as exc:
        await _push(session_id, type="error", text=f"执行失败：{exc}")


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("synapse_yield.web.app:app", host=s.web_host, port=s.web_port, reload=True)
