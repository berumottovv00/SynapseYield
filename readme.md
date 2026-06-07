# SynapseYield

SynapseYield 是一个面向量化交易实验的 Harness 架构项目，目标是在严格可控、可审计、可回放的框架内，接入本地模拟盘与长桥证券 OpenAPI，完成从行情感知、策略决策、风控校验到订单执行、成交回报和资产对账的完整闭环。

当前阶段优先建设本地模拟盘与交易 Harness，不直接开启真实资金自动交易。长桥证券接入应作为 Broker Adapter 逐步引入，并在合规、权限、账户、地域限制确认后再开放真实下单能力。

## 设计原则

1. Harness 统一管控

   所有行情、策略、风控、下单、回报、日志、状态更新都必须经过 Harness 编排。任何 Agent 或 Skill 都不能绕过 Harness 直接触发交易。

2. Agent 做编排，Skill 做工具

   Agent 可以负责流程判断、异常处理、上下文归纳和任务协作；Skill 必须保持标准化、确定性、可测试，负责单一工具能力，例如获取行情、调用策略、提交订单、查询仓位。

3. 风控与交易状态必须确定性

   下单数量、价格、账户资金、仓位、订单状态、风控规则不能依赖 LLM 自由推理。核心交易链路应由状态机、规则引擎、幂等机制和数据库事务保证。

4. 本地模拟盘优先

   第一阶段先用本地模拟盘验证完整生命周期，包括下单、成交、撤单、资金扣减、仓位更新、成交记录、对账和回放。长桥接入放在 Adapter 层，避免业务逻辑被券商 SDK 绑定。

5. 可追溯、可补偿、可恢复

   每一次信号、决策、风控、下单、成交、驳回、撤单、异常都要记录审计日志。外部 API 无法真正回滚，因此真实交易场景必须依赖订单状态机、幂等键、补偿任务和定时对账。

## 总体架构

```text
┌─────────────────────────────────────────────────────────────┐
│                         Harness Core                         │
│  调度编排 | 状态机 | 风控入口 | 幂等控制 | 审计日志 | 对账补偿 │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                         Agent Layer                          │
│  Market Agent | Strategy Agent | Risk Agent | Execution Agent │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                         Skill Layer                          │
│  Quote Skill | Strategy Adapter | Risk Rules | Broker Skill   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                       Broker / Data Layer                    │
│  Local Simulator | Longbridge Adapter | Market Data Source    │
└─────────────────────────────────────────────────────────────┘
```

推荐的工程分层：

```text
synapse_yield/
  harness/        # 中央编排、事件流、任务状态、审计日志
  agents/         # Agent 逻辑角色，不直接访问外部接口
  skills/         # 标准化工具接口和适配器
  strategy/       # 现有量化策略 Skill 的适配层
  risk/           # 风控规则引擎、规则配置、拦截原因
  broker/         # LocalSim 与 Longbridge Adapter
  ledger/         # 资金、仓位、成交、订单账本
  reconcile/      # 对账、补偿、异常恢复
  storage/        # 数据模型、仓储接口、迁移脚本
  tests/          # 单元测试、集成测试、回放测试
```

## 核心数据流

```text
1. Market Agent 定时触发行情任务
2. Harness 调用 Quote Skill 获取行情
3. Harness 将标准化 MarketSnapshot 发送给 Strategy Agent
4. Strategy Agent 调用 Strategy Adapter Skill 生成 TradeSignal
5. Harness 将 TradeSignal 转换为 OrderIntent
6. Risk Agent 调用 Risk Rules 对 OrderIntent 进行校验
7. Harness 将通过风控的 OrderIntent 固化为 OrderRequest
8. Execution Agent 调用 Broker Skill 提交订单
9. Broker Adapter 返回提交结果或订单事件
10. Harness 更新订单状态机、资金仓位账本、审计日志
11. Reconcile Job 定时对账并处理不一致状态
```

## Agent 分工

Agent 是逻辑角色，不等于必须使用 LLM。高频、确定性、可重复的行为应优先用普通服务实现；LLM Agent 只用于低频解释、异常归因、策略说明和人工辅助决策。

### Market Agent

职责：

- 按交易时段和配置频率触发行情获取。
- 维护上一次行情快照，用于判断突破、放量、波动异常等事件。
- 将行情事件交给 Harness，不直接调用策略或下单接口。

输入：

- 股票代码列表。
- 刷新频率。
- 行情字段配置。
- 交易日历。

输出：

- `MarketSnapshot`：标准化行情快照。
- `MarketEvent`：突破、放量、异常波动等事件。

### Strategy Agent

职责：

- 接收 Harness 分发的行情快照或行情事件。
- 调用已有量化策略 Skill 的适配层。
- 将策略输出标准化为 `TradeSignal` 或 `OrderIntent`。
- 对策略输出进行基本格式检查，不做最终风控。

输入：

- `MarketSnapshot`
- `MarketEvent`
- 策略配置。
- 当前账户摘要，只用于策略上下文，不用于最终风控判定。

输出：

- `TradeSignal`：买卖方向、置信度、理由、目标仓位等。
- `OrderIntent`：拟下单标的、方向、数量、价格类型、有效期等。

### Risk Agent

职责：

- 对 `OrderIntent` 进行确定性风控校验。
- 读取账户资金、持仓、当日成交、当日亏损、未完成订单等状态。
- 返回通过、驳回或需要人工确认。
- 给出结构化驳回原因，便于审计和回放。

基础规则：

- 单票最大仓位。
- 总仓位上限。
- 单笔订单最大金额。
- 单日最大亏损。
- 单日最大交易次数。
- 重复下单冷却时间。
- 禁止在非交易时段下单。
- 限价单价格偏离盘口上限。
- 止损、止盈、移动止损规则。

输出：

- `RiskDecision(APPROVED)`
- `RiskDecision(REJECTED)`
- `RiskDecision(REQUIRES_MANUAL_REVIEW)`

### Execution Agent

职责：

- 只接收 Harness 已通过风控并签名的 `OrderRequest`。
- 调用 Broker Skill 提交订单、撤单或查询订单。
- 处理提交失败、超时、重复提交、券商返回不确定状态等异常。
- 将结果回传 Harness，由 Harness 更新订单状态机。

禁止事项：

- 不允许自行修改下单数量。
- 不允许自行绕过风控重新提交。
- 不允许直接操作资金和仓位账本。

## Skill 设计

Skill 是标准化工具层，接口稳定、入参出参明确、可单独测试。所有 Skill 都应支持本地 mock，便于回放和集成测试。

### Quote Skill

作用：

- 获取实时行情。
- 获取历史 K 线。
- 订阅行情推送。
- 将不同数据源格式统一为内部模型。

标准输出：

```json
{
  "symbol": "AAPL.US",
  "timestamp": "2026-05-30T09:30:00Z",
  "last_price": 180.12,
  "open": 178.5,
  "high": 181.0,
  "low": 177.9,
  "volume": 1234567,
  "turnover": 22222222.22,
  "bid_price": 180.1,
  "ask_price": 180.13,
  "source": "local_sim"
}
```

### Strategy Adapter Skill

作用：

- 包装已有量化交易 Skill。
- 不修改原策略源码。
- 将 Harness 的标准行情输入转换为策略需要的格式。
- 将策略输出转换为标准 `TradeSignal` 或 `OrderIntent`。

标准输出：

```json
{
  "signal_id": "sig_20260530_000001",
  "symbol": "AAPL.US",
  "side": "BUY",
  "confidence": 0.72,
  "target_quantity": 10,
  "order_type": "LIMIT",
  "limit_price": 180.1,
  "reason": "momentum_breakout",
  "strategy_name": "example_strategy",
  "strategy_version": "v1"
}
```

### Risk Rules Skill

作用：

- 执行确定性规则校验。
- 返回结构化结果。
- 支持配置化规则。
- 支持 dry run 和回放。

标准输出：

```json
{
  "decision": "REJECTED",
  "reason_code": "MAX_POSITION_EXCEEDED",
  "message": "Order would exceed max position for AAPL.US",
  "checked_rules": [
    "market_session",
    "cash_available",
    "max_position",
    "daily_loss_limit"
  ]
}
```

### Broker Skill

作用：

- 提交订单。
- 撤销订单。
- 查询订单。
- 查询持仓。
- 查询账户资金。
- 接收成交回报。

Broker Skill 只暴露统一接口，底层可以是本地模拟盘，也可以是长桥证券。

```text
BrokerAdapter
  submit_order(order_request) -> BrokerOrderResult
  cancel_order(order_id) -> BrokerCancelResult
  get_order(order_id) -> BrokerOrderSnapshot
  list_positions() -> PositionSnapshot[]
  get_account_balance() -> AccountBalance
  subscribe_order_events(handler) -> void
  subscribe_quote_events(symbols, handler) -> void
```

## 长桥证券接入方案

长桥接入只放在 `broker/longbridge` 和 `skills/broker` 适配层，不进入策略、风控和账本核心逻辑。

### 接入能力

长桥 OpenAPI 可用于：

- 实时行情获取。
- 行情推送订阅。
- 历史 K 线获取。
- 提交订单。
- 撤销订单。
- 查询订单状态。
- 查询账户、资金和持仓。
- 接收交易推送和成交回报。

### 内部标的格式

内部统一使用带市场后缀的 symbol：

```text
AAPL.US
TSLA.US
00700.HK
09988.HK
```

如果长桥 SDK 对 symbol 格式有额外要求，在 Longbridge Adapter 内部转换，其他模块不感知。

### 下单适配

内部 `OrderRequest`：

```json
{
  "client_order_id": "ord_20260530_000001",
  "symbol": "AAPL.US",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": 10,
  "limit_price": 180.1,
  "time_in_force": "DAY",
  "source_signal_id": "sig_20260530_000001",
  "risk_decision_id": "risk_20260530_000001"
}
```

Longbridge Adapter 负责转换为长桥 SDK 的提交订单参数，并记录：

- 内部订单号 `client_order_id`
- 券商订单号 `broker_order_id`
- 请求参数快照
- 响应参数快照
- 提交时间
- 错误码和错误信息

### 推送与回报

交易推送进入系统后，不直接修改仓位。推荐流程：

```text
Longbridge Trade Push
  -> Broker Event Normalizer
  -> Harness Event Bus
  -> Order State Machine
  -> Ledger Update
  -> Audit Log
  -> Notification / UI
```

这样可以保证所有状态变化都由 Harness 统一记录和回放。

## 本地模拟盘设计

本地模拟盘是第一阶段核心，用于验证系统完整性。

### 账户模型

```text
Account
  account_id
  base_currency
  cash_available
  cash_frozen
  equity
  realized_pnl
  unrealized_pnl
  created_at
  updated_at
```

### 订单模型

```text
Order
  order_id
  client_order_id
  broker_order_id
  symbol
  side
  order_type
  status
  quantity
  filled_quantity
  limit_price
  avg_fill_price
  time_in_force
  source_signal_id
  risk_decision_id
  idempotency_key
  created_at
  updated_at
```

### 成交模型

```text
Fill
  fill_id
  order_id
  symbol
  side
  quantity
  price
  commission
  fill_time
```

### 仓位模型

```text
Position
  account_id
  symbol
  quantity
  available_quantity
  avg_cost
  market_price
  market_value
  unrealized_pnl
  updated_at
```

### 资金流水模型

```text
CashLedger
  ledger_id
  account_id
  order_id
  fill_id
  event_type
  amount
  currency
  balance_after
  created_at
```

### 撮合规则

第一版可采用简单规则：

- 市价单按最新价成交。
- 买入限价单：当 `limit_price >= ask_price` 时成交。
- 卖出限价单：当 `limit_price <= bid_price` 时成交。
- 支持全部成交，后续再扩展部分成交。
- 佣金、滑点、印花税等费用先配置化，默认可为 0。

第二版再增加：

- 部分成交。
- 盘口深度。
- 滑点模型。
- 延迟成交。
- 不同市场交易规则。
- 港股、美股交易时段和最小交易单位。

## 订单状态机

订单状态机是核心，不建议用分布式事务替代。

```text
CREATED
  -> RISK_REJECTED
  -> RISK_APPROVED
  -> SUBMITTING
  -> SUBMITTED
  -> PARTIALLY_FILLED
  -> FILLED
  -> CANCEL_PENDING
  -> CANCELLED
  -> FAILED
  -> RECONCILING
```

状态说明：

- `CREATED`：Harness 已生成订单意图。
- `RISK_REJECTED`：风控驳回，不允许提交。
- `RISK_APPROVED`：风控通过，可以提交。
- `SUBMITTING`：正在提交给 Broker。
- `SUBMITTED`：Broker 已接收。
- `PARTIALLY_FILLED`：部分成交。
- `FILLED`：全部成交。
- `CANCEL_PENDING`：撤单请求已发出。
- `CANCELLED`：撤单成功。
- `FAILED`：明确失败。
- `RECONCILING`：本地状态与 Broker 状态不确定，等待对账。

关键约束：

- 只有 Harness 可以驱动状态迁移。
- 状态迁移必须写审计日志。
- 每次外部提交必须带幂等键。
- 对于超时和未知结果，进入 `RECONCILING`，不能简单标记失败。

## 一致性与事务设计

### 本地模拟盘

本地模拟盘优先使用单库事务保证一致性：

```text
begin transaction
  insert order
  freeze cash
  insert audit log
commit
```

成交时：

```text
begin transaction
  update order filled quantity and status
  insert fill
  update position
  insert cash ledger
  update account cash
  insert audit log
commit
```

### 长桥真实或模拟 API

外部 Broker API 无法被本地数据库事务回滚，因此不应把 Seata AT 当作核心方案。推荐使用：

- 订单状态机。
- `client_order_id` 幂等键。
- Outbox 本地消息表。
- 定时对账任务。
- Broker 订单查询补偿。
- 必要时自动撤单或人工确认。

提交订单推荐流程：

```text
1. 本地创建 Order，状态为 RISK_APPROVED
2. 写入 Outbox 事件 ORDER_READY_TO_SUBMIT
3. Execution Agent 读取事件并提交 Broker
4. 提交前将状态置为 SUBMITTING
5. Broker 返回成功，记录 broker_order_id，状态置为 SUBMITTED
6. Broker 返回失败，状态置为 FAILED
7. Broker 超时或结果不明，状态置为 RECONCILING
8. Reconcile Job 查询 Broker 订单状态并修正本地状态
```

成交回报推荐流程：

```text
1. 收到 Broker 成交事件
2. 按 broker_order_id 和 fill_id 去重
3. 写入 BrokerEvent 原始事件
4. 驱动订单状态机
5. 在本地事务中更新 order、fill、position、cash_ledger
6. 写入 Outbox 事件 POSITION_UPDATED / ORDER_FILLED
7. 下游通知或 UI 从 Outbox / Kafka 消费
```

### Kafka 使用边界

第一版可以不引入 Kafka。只有当系统拆成多个服务，或需要多端通知、异步风控、IM 推送、跟单分发时，再引入 Kafka。

适合 Kafka 的事件：

- `ORDER_SUBMITTED`
- `ORDER_FILLED`
- `ORDER_CANCELLED`
- `RISK_REJECTED`
- `POSITION_UPDATED`
- `ACCOUNT_EQUITY_UPDATED`
- `RECONCILE_REQUIRED`

对于核心账本更新，Kafka 不能替代数据库事务。Kafka 适合分发事件，本地数据库仍是事实来源。

### Seata 使用边界

Seata AT 适合多个内部服务、多个内部数据库之间的一致性管理，例如账户服务、订单服务、仓位服务都由自己控制，并且都接入 Seata。

不建议用 Seata 解决以下问题：

- 券商 API 下单成功但本地写库失败。
- 券商成交回报已发生但本地处理失败。
- 外部 Broker 订单状态与本地状态不一致。

这些场景应通过状态机、对账、补偿和人工兜底处理。

## 风控设计

风控输入：

```json
{
  "account": {
    "cash_available": 100000,
    "equity": 120000,
    "daily_realized_pnl": -500
  },
  "positions": [
    {
      "symbol": "AAPL.US",
      "quantity": 20,
      "market_value": 3600
    }
  ],
  "open_orders": [],
  "order_intent": {
    "symbol": "AAPL.US",
    "side": "BUY",
    "quantity": 10,
    "order_type": "LIMIT",
    "limit_price": 180.1
  }
}
```

风控输出：

```json
{
  "risk_decision_id": "risk_20260530_000001",
  "decision": "APPROVED",
  "reason_code": "OK",
  "message": "Risk checks passed",
  "checked_at": "2026-05-30T09:30:01Z"
}
```

配置示例：

```yaml
risk:
  max_position_ratio_per_symbol: 0.2
  max_total_position_ratio: 0.8
  max_single_order_value: 10000
  max_daily_loss: 3000
  max_daily_order_count: 50
  duplicate_order_cooldown_seconds: 60
  max_limit_price_deviation_ratio: 0.03
  require_market_session: true
```

## 审计日志

每个关键动作都要落审计日志：

```text
AuditLog
  audit_id
  trace_id
  actor_type
  actor_name
  event_type
  input_snapshot
  output_snapshot
  previous_state
  next_state
  created_at
```

必须记录的事件：

- 行情快照生成。
- 策略信号生成。
- 风控通过或驳回。
- 订单创建。
- 订单提交。
- 券商响应。
- 成交回报。
- 撤单请求。
- 资金变化。
- 仓位变化。
- 对账修复。
- 异常和重试。

## 幂等与重试

幂等键建议：

```text
idempotency_key = hash(account_id + strategy_name + signal_id + symbol + side + quantity + price + trading_day)
```

要求：

- 相同幂等键不能重复创建有效订单。
- Broker 提交超时后不能盲目再次提交。
- 重试前先查询本地订单状态和 Broker 订单状态。
- 所有重试都必须写审计日志。

## 对账与补偿

对账任务：

- 定时查询 Broker 订单列表。
- 定时查询 Broker 持仓。
- 定时查询 Broker 账户资金。
- 对比本地 `orders`、`positions`、`cash_ledger`。
- 对不一致项生成 `RECONCILE_REQUIRED` 事件。

补偿策略：

- 本地缺订单，Broker 有订单：补录订单并标记来源为 reconcile。
- 本地订单状态落后：按 Broker 状态推进状态机。
- 本地有成交缺流水：补录成交和资金流水。
- 本地与 Broker 差异无法自动判断：进入人工确认队列。

## 推荐里程碑

### Milestone 1：文档与模型

- 完成 README 架构设计。
- 确定内部数据模型。
- 确定 Agent 与 Skill 边界。
- 确定订单状态机。

### Milestone 2：本地模拟盘

- 实现本地账户、订单、成交、仓位、资金流水。
- 实现本地撮合规则。
- 实现基础风控规则。
- 实现审计日志。

当前状态：已完成。

已支持：

- 市价单按买一/卖一或最新价成交。
- 限价单立即成交或保持待成交，并可由后续行情再次撮合。
- 买单冻结现金、卖单冻结可用持仓，成交或撤单后正确结算/释放。
- 成交后更新账户权益、持仓成本、已实现/未实现盈亏和资金流水。
- 配置化佣金费率与最低佣金，默认费用为 0。
- 模拟盘订单接收、成交和撤单 Broker 原始事件。
- `ORDER_FILLED`、`ORDER_CANCELLED`、`POSITION_UPDATED` 等 Outbox 事件。
- 单笔金额、现金、可用持仓、单票/总仓位、亏损阈值、每日订单数、
  重复订单、冷却时间、价格偏离和交易时段开关等基础风控。
- 订单状态变化、资源冻结/释放和账本更新审计日志。

### Milestone 3：策略适配

- 封装已有量化策略 Skill。
- 标准化行情输入和策略输出。
- 支持策略回放。
- 支持 dry run。

### Milestone 4：Harness 编排

- 串联行情、策略、风控、执行。
- 引入 trace id。
- 引入幂等键。
- 引入订单状态机。

### Milestone 5：长桥 Adapter

- 接入长桥行情查询。
- 接入长桥行情推送。
- 接入长桥账户、持仓、订单查询。
- 接入长桥下单和撤单。
- 接入长桥交易推送。
- 默认只允许 dry run 或模拟环境。

### Milestone 6：对账与异常恢复

- 实现订单对账。
- 实现资金仓位对账。
- 实现未知状态恢复。
- 实现人工确认队列。

## 开发启动

当前项目采用 Python 单体应用起步：

```text
Python >= 3.12
MySQL >= 8.0
SQLAlchemy 2.x
Alembic
PyMySQL
Pydantic Settings
Pytest
```

### 本地环境

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`.env` 示例：

```bash
APP_ENV=dev
DATABASE_URL=mysql+pymysql://synapse:password@127.0.0.1:3306/synapse_yield
ENABLE_LIVE_TRADING=false
BASE_CURRENCY=USD
```

### MySQL 建库

需要你本地 MySQL 准备好后执行：

```sql
CREATE DATABASE synapse_yield CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'synapse'@'localhost' IDENTIFIED BY 'password';
GRANT ALL PRIVILEGES ON synapse_yield.* TO 'synapse'@'localhost';
FLUSH PRIVILEGES;
```

如果你想使用已有 MySQL 用户，只需要改 `.env` 里的 `DATABASE_URL`。

### 执行迁移

建库完成并确认 `.env` 配置正确后执行：

```bash
alembic upgrade head
```

当前首个迁移会创建这些表：

```text
accounts
positions
orders
fills
cash_ledger
risk_decisions
audit_logs
outbox_events
broker_events
reconcile_tasks
strategy_signals
market_snapshots
```

### 基础校验

```bash
python3 -m compileall synapse_yield tests migrations
pytest
```

### 本地模拟盘冒烟测试

建表完成后，可以运行本地模拟盘端到端 demo：

```bash
python -m synapse_yield.demo.local_sim_trade
```

该 demo 会执行一次完整的本地模拟交易：

```text
1. 创建或读取 demo_account
2. 创建 demo 策略信号
3. 生成买入 AAPL.US 的限价订单
4. 执行基础风控
5. 风控通过后写入 outbox_events
6. 提交给 LocalSimBroker
7. 按模拟 bid/ask 立即成交
8. 更新订单、成交、持仓和资金流水
```

成功输出示例：

```text
local sim trade completed
order_status=FILLED
filled_quantity=10
avg_fill_price=180.05
fills=1
cash_ledger_entries=1
```

运行后可在 Navicat 中查看：

```text
accounts
strategy_signals
risk_decisions
orders
fills
cash_ledger
positions
audit_logs
outbox_events
```

当前已实现：

- 项目骨架与配置读取。
- SQLAlchemy 数据模型。
- Alembic 初始建表迁移。
- 订单状态机。
- 基础风控引擎，覆盖金额、资金、持仓、仓位比例、亏损、频次、重复单、
  行情价格偏离和交易时段开关。
- Harness 订单服务入口。
- 本地模拟盘 Broker，支持资源冻结、即时/延迟撮合、撤单、佣金、盈亏、
  Broker 事件、Outbox 事件以及资金和持仓账本更新。
- 本地模拟盘端到端 demo。
- 状态机、风控和本地模拟盘端到端测试。

## 合规与安全边界

本项目应默认用于个人研究、本地模拟盘和策略验证。接入真实券商账户前必须确认：

- 长桥账户是否允许当前地区、当前身份、当前用途接入。
- API 权限是否包含交易权限。
- 是否允许自动化交易。
- 是否需要额外授权、备案或合规评估。
- 是否面向他人提供荐股、跟单、代客理财或跨境交易服务。

安全要求：

- API Key 不得写入代码仓库。
- API Secret 只能通过环境变量或密钥管理系统读取。
- 真实交易开关必须默认关闭。
- 真实下单需要显式配置，例如 `ENABLE_LIVE_TRADING=true`。
- 生产环境建议增加人工确认或白名单账户。

## 简历描述参考

可写为：

```text
设计量化交易 Harness 架构，围绕行情、策略、风控、下单和成交回报构建可审计的自动化交易闭环；通过订单状态机、幂等提交、Outbox 事件表和定时对账机制解决下单、资金、仓位跨模块一致性问题，并预留长桥证券 OpenAPI Adapter 以支持后续券商接入。
```

如果后续引入 Kafka，可扩展为：

```text
在成交回报和多端状态同步场景中引入 Kafka 事件流，并结合本地消息表实现最终一致性，保障订单、资金、仓位和通知链路可追溯、可补偿、可恢复。
```
