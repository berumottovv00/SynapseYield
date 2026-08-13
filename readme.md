# SynapseYield

Harness 驱动的量化交易模拟器：本地风控/订单状态机 + 可插拔 Broker（本地模拟盘 / 长桥真实接口）+ LangGraph 聊天 Agent 前端。附带一个独立的多 Agent 新闻/技术面选股工具。

仓库里有两个彼此独立、互不 import 的子系统：

1. **交易内核 + Web 聊天端**（`synapse_yield/` 下除 `agent/` 外的所有包）—— 本 README 的主体。
2. **每日选股 Swarm 工具**（`synapse_yield/agent/`）—— 一个独立打包的多 Agent 框架，只通过自己的 CLI 入口运行，见本文末尾说明。

## 目录结构

```
synapse_yield/
├── domain/       # 纯领域层：枚举、ID 生成、Pydantic Schema、订单状态机
├── storage/      # SQLAlchemy ORM 模型 + Session（MySQL，Alembic 管理迁移）
├── risk/         # 下单前风控引擎
├── harness/      # OrderService（编排下单意图→风控→状态机→审计→Outbox）
├── broker/       # Broker 适配层：local_sim（本地模拟盘）/ longbridge（长桥 OpenAPI）
├── market/       # 行情拉取（长桥历史 K 线 → LLM 可读 markdown）
├── tools/        # 供闲聊节点调用的 LLM 工具（MarketHistoryTool 等）
├── skills/       # SelectStocksSkill：调用 LLM Provider 做选股
├── agents/       # LangGraph 编排层：HarnessAgentGraph + LLM Provider
├── web/          # FastAPI + WebSocket 聊天服务，静态前端
├── config.py     # pydantic-settings，读取 .env
└── agent/        # 独立子系统，见下文，与以上包无任何 import 关系

migrations/       # Alembic 迁移脚本
tests/            # pytest 测试
```

## 核心数据流

```
浏览器 (web/static) ──WebSocket──▶ web/app.py
                                     │
                                     ▼
                          agents/harness_agent.py (LangGraph)
                          interpret → select / order / chat
                              │              │
                    skills/select_stocks.py  │
                    (LLM 选股，可调用          │
                     market/history 工具)     │
                              │              │
                              ▼              ▼
                       人工审批(interrupt) → harness/trade_executor.py
                                                  │
                                    harness/order_service.py
                                    (风控 risk/engine.py → 状态机 domain/state_machine.py)
                                                  │
                                                  ▼
                                    broker/factory.py → local_sim | longbridge
                                                  │
                                                  ▼
                                    storage/models.py (MySQL，SQLAlchemy)
```

- **`domain/state_machine.py`**：集中定义订单状态迁移合法性（`CREATED → RISK_APPROVED/RISK_REJECTED → SUBMITTING → SUBMITTED → FILLED/CANCELLED/...`），业务代码统一调用 `assert_order_transition` 校验，禁止绕过。
- **`risk/engine.py`**：下单前同步规则校验（账户存在性、行情匹配、资金/仓位等），返回 `APPROVED / REJECTED / REQUIRES_MANUAL_REVIEW`。
- **`broker/factory.py`**：按 `.env` 的 `BROKER_TYPE` 选择 Broker 实现——`local_sim`（不连外部服务，用于开发测试）或 `longbridge`（真实/模拟账户，三道开关默认关闭：`ENABLE_EXTERNAL_ORDER_SUBMISSION` / `ENABLE_LIVE_TRADING` / `LONGBRIDGE_MODE`）。
- **`agents/harness_agent.py`**：唯一的人机交互点是 LangGraph 的 `interrupt()`——选股结果必须经用户审批（WebSocket `approve` 消息）才会进入 `TradeExecutor` 下单，LLM 只能建议、不能直接下单。
- **`storage/models.py`**：账户、持仓、订单、成交、风控决策、审计日志、Outbox 事件、对账任务、行情快照等 12 张表，通过 Alembic 管理迁移（`migrations/versions/`）。

## 运行

```bash
# 安装依赖（含 dev：pytest / ruff）
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env   # 按需填写 DATABASE_URL / BROKER_TYPE / OPENAI_API_KEY 等

# 数据库迁移
alembic upgrade head

# 启动聊天服务
python -m synapse_yield.web.app   # 或 uvicorn synapse_yield.web.app:app --reload

# 测试
pytest
```

默认 `BROKER_TYPE=local_sim`、`ENABLE_LLM_TRADING_AGENT=false`——本地全离线可跑；接入长桥或 LLM 选股需要在 `.env` 里显式打开对应开关和填写密钥。

## 每日选股 Swarm 工具（`synapse_yield/agent/`）

一个独立打包的多 Agent 框架（自带 `src/agent`、`src/swarm`、`src/skills` 等运行时），从更大的多 preset 版本裁剪而来，此仓库中只保留了单个 preset **`news_technical_stock_picker`**：新闻催化剂分析师 + 技术面扫描 Agent 并行运行，再由选股策略 Agent 交叉确认产出排序买入列表。

```bash
python synapse_yield/agent/run_daily_pick.py --market "A-shares" --num-picks 5
```

运行产物（任务日志、报告）写入 `synapse_yield/agent/.swarm/runs/`，为运行时数据，不纳入版本控制。它与仓库其余部分完全独立，不被 `synapse_yield/*` 的任何模块引用。
