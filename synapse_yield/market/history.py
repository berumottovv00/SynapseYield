"""从长桥拉取历史 K 线并格式化为 LLM 可读的 markdown 表格。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class CandlestickBar(BaseModel):
    """单根 K 线数据。"""

    model_config = ConfigDict(frozen=True)

    date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    turnover: Decimal = Field(ge=0)


class HistoryFetcher(Protocol):
    """拉取一支股票历史 K 线并返回 markdown 的接口，供测试注入 Fake。"""

    def fetch_markdown(self, symbol: str) -> str: ...


class LongbridgeHistoryFetcher:
    """通过长桥 SDK 拉取日 K 线并格式化为 markdown 表格。"""

    def __init__(
        self,
        *,
        gateway=None,
        period: str = "Day",
        count: int = 60,
        adjust: str = "NoAdjust",
    ):
        """
        gateway: LongbridgeSDKGateway 实例，None 时自动创建。
        period:  K 线周期名称，对应 longbridge.openapi.Period 枚举成员名，默认 "Day"。
        count:   拉取根数，默认 60 根（约 3 个月日 K）。
        adjust:  复权类型，"NoAdjust" 或 "ForwardAdjust"。
        """
        self.period_name = period
        self.count = count
        self.adjust_name = adjust

        if gateway is None:
            from synapse_yield.broker.longbridge.gateway import LongbridgeSDKGateway

            gateway = LongbridgeSDKGateway()
        self._gateway = gateway

    def fetch_markdown(self, symbol: str) -> str:
        """拉取历史 K 线，返回格式化的 markdown 段落（含表格）。"""
        bars = self._fetch_bars(symbol)
        if not bars:
            return f"## {symbol} 历史行情\n\n*暂无数据*\n"
        return _format_markdown(symbol, bars, self.period_name)

    def _fetch_bars(self, symbol: str) -> list[CandlestickBar]:
        raw_bars = self._gateway.history_candlesticks(
            symbol,
            period=self.period_name,
            count=self.count,
            adjust=self.adjust_name,
        )
        result: list[CandlestickBar] = []
        for bar in raw_bars:
            ts = getattr(bar, "timestamp", None)
            if ts is None:
                continue
            bar_date = ts.date() if isinstance(ts, datetime) else ts
            result.append(
                CandlestickBar(
                    date=bar_date,
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.close)),
                    volume=Decimal(str(bar.volume)),
                    turnover=Decimal(str(bar.turnover)),
                )
            )
        result.sort(key=lambda b: b.date)
        return result


def _format_markdown(symbol: str, bars: list[CandlestickBar], period_name: str) -> str:
    """把 K 线列表格式化为 LLM 可直接阅读的 markdown。"""
    period_label = {"Day": "日", "Week": "周", "Month": "月"}.get(period_name, period_name)
    latest = bars[-1]
    first = bars[0]

    # 统计摘要
    closes = [b.close for b in bars]
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    pct_change = (latest.close - first.close) / first.close * 100

    lines = [
        f"## {symbol} 历史{period_label}K 线（最近 {len(bars)} 根）",
        "",
        f"- 区间：{first.date} → {latest.date}",
        f"- 最新收盘：{latest.close}",
        f"- 区间最高：{high}　最低：{low}",
        f"- 区间涨跌幅：{pct_change:+.2f}%",
        "",
        "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    # 只显示最近 30 根避免 token 过多
    for bar in bars[-30:]:
        lines.append(
            f"| {bar.date} | {bar.open} | {bar.high} | {bar.low} | {bar.close} | {bar.volume} |"
        )
    lines.append("")
    return "\n".join(lines)
