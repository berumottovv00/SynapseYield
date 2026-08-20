import json
from typing import Protocol

from synapse_yield.config import get_settings
from synapse_yield.domain.schemas import StockSelectionResult

DEFAULT_SYSTEM_PROMPT = (
    "You are an experienced equity analyst and intraday portfolio manager. "
    "Read the supplied market research report and historical price data, then identify exactly {n} stocks "
    "most worth trading TODAY (intraday) based on the analysis. "
    "For each pick:\n"
    "- symbol: exact ticker from the report with market suffix (e.g. AAPL.US, 00700.HK)\n"
    "- action: BUY or SELL only — never HOLD\n"
    "- confidence: decimal 0.0-1.0 (e.g. 0.85 = high, 0.60 = moderate) — never words\n"
    "- quantity: recommended share count as a positive integer, calculated from the "
    "account's cash_available and the price mentioned in the report; "
    "size each position at 10-20% of cash_available unless instructions say otherwise; "
    "check existing positions to avoid over-concentration; set 0 if price is unknown\n"
    "- suggested_limit_price: reference a price from the report or historical data; null if unknown; "
    "must be a plain number without currency symbols (e.g. 10.50 not $10.50)\n"
    "- take_profit_price: intraday take-profit price for BUY — use resistance levels, target price, "
    "or 2-5% above entry from the report/history; null if action is SELL\n"
    "- stop_loss_price: intraday stop-loss price for BUY — use support levels or 1-3% below entry "
    "from the report/history; null if action is SELL; must be strictly below suggested_limit_price\n"
    "- rationale: cite specific evidence from the report and historical price context (max 80 words)\n"
    "- key_catalysts: list of drivers from the report (max 5 items, each under 15 words)\n"
    "- risk_notes: always provide, use [] only if truly no risks (max 3 items)\n"
    "Rank picks by conviction descending. Do not invent facts not in the report or history data. "
    "Keep all text fields concise to stay within token limits."
)


class TradeProposalProvider(Protocol):
    """生成结构化交易建议的模型提供方接口。"""

    provider_name: str
    model_name: str


class OpenAITradeProposalProvider:
    """使用 OpenAI Chat API 结构化输出生成交易建议。"""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client=None,
    ):
        settings = get_settings()
        configured_key = settings.openai_api_key
        resolved_key = api_key or (
            configured_key.get_secret_value() if configured_key is not None else None
        )
        if client is None and not resolved_key:
            raise RuntimeError("OPENAI_API_KEY is required for the LLM trading Agent")

        if client is None:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "OpenAI SDK is required. Install project dependencies first."
                ) from exc
            client = OpenAI(api_key=resolved_key)

        self.client = client
        self.model_name = model or settings.openai_model

    def select_stocks(
        self,
        market_content: str,
        account_context: dict,
        n: int = 1,
        instructions: str | None = None,
        custom_prompt: str | None = None,
        tools: list | None = None,
    ) -> StockSelectionResult:
        """从调研报告中挑选最值得操作的 n 支股票，结合账户仓位给出下单数量建议。

        若传入 tools，先跑 ReAct 循环让 LLM 按需拉取行情数据，
        所有 tool call 执行完毕后再做一次结构化输出。
        """
        if custom_prompt:
            system_prompt = custom_prompt.replace("{n}", str(n))
        else:
            system_prompt = DEFAULT_SYSTEM_PROMPT.replace("{n}", str(n))

        if instructions:
            system_prompt += f"\nAdditional operator instructions:\n{instructions}"

        user_content = (
            f"Account & Positions:\n{json.dumps(account_context, ensure_ascii=False)}\n\n"
            f"Market Research Report:\n{market_content}"
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # ── ReAct 循环：LLM 可按需调用工具拉取行情 ──────────────────────────────
        if tools:
            tool_map = {t.name: t for t in tools}
            tool_schemas = [t.schema for t in tools]

            for _ in range(5):  # 最多 5 轮，防止死循环
                resp = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice="auto",
                    max_tokens=2048,
                )
                msg = resp.choices[0].message
                # 把 assistant 消息追加到历史（含 tool_calls 字段）
                messages.append(msg.model_dump(exclude_unset=True))

                if not msg.tool_calls:
                    break  # LLM 不再调工具，退出循环

                # 执行所有 tool call，把结果追加到 messages
                for tc in msg.tool_calls:
                    tool = tool_map.get(tc.function.name)
                    result = tool.run_call(tc) if tool else f"Unknown tool: {tc.function.name}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })

        # ── 最终结构化输出 ────────────────────────────────────────────────────────
        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=messages,
            response_format=StockSelectionResult,
            max_tokens=4096,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                "LLM 响应被截断（超出 max_tokens），请缩短报告内容或减少选股数量。"
            )
        if getattr(choice.message, "refusal", None):
            raise RuntimeError(f"LLM 拒绝回答：{choice.message.refusal}")
        result = choice.message.parsed
        if result is None:
            raise RuntimeError("OpenAI 未返回有效的选股结果，请重试。")
        return result