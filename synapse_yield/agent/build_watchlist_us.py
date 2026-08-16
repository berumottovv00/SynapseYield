#!/usr/bin/env python3
"""挑选当日美股候选股 watchlist，供 run_daily_pick.py --market US --watchlist 使用。

不调用 LLM：用 yfinance 内置的官方预设 screener（Yahoo Finance 数据，免 key，
不依赖网页爬取）拉「当日涨幅榜」「成交量榜」「高做空比例榜」三类数据做机械筛选。

逻辑和 A 股版 build_watchlist.py 一致（多路信号合并去重，跨维度命中排前面），
只是把 A 股专属的涨停/龙虎榜/资金流向换成了美股市场结构下的等价信号——美股
没有涨跌停板，也没有龙虎榜式的机构游资席位披露，最接近的免费信号是涨幅榜、
成交量榜和做空比例榜。

用法：
    python synapse_yield/agent/build_watchlist_us.py
    python synapse_yield/agent/build_watchlist_us.py --limit 20 --verbose

    python synapse_yield/agent/run_daily_pick.py --market US \
        --watchlist "$(python synapse_yield/agent/build_watchlist_us.py)"
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import yfinance as yf

_TOP_N_PER_SOURCE = 15
_SCREEN_COUNT = 50  # 每个 screener 多取一些，供排序后截断


def _warn(source: str, exc: Exception) -> None:
    print(f"[build_watchlist_us] {source} 拉取失败，跳过（{exc!r}）", file=sys.stderr)


def _quotes(query: str) -> list[dict]:
    try:
        res = yf.screen(query, count=_SCREEN_COUNT)
    except Exception as exc:
        _warn(query, exc)
        return []
    return res.get("quotes", [])


def _day_gainers_candidates() -> list[tuple[str, str, str]]:
    """当日涨幅榜：美股没有涨停板，用涨幅榜近似 A 股涨停股的"价格异动+资金抢筹"。"""
    quotes = _quotes("day_gainers")
    ranked = sorted(quotes, key=lambda q: q.get("regularMarketChangePercent") or 0, reverse=True)
    return [
        (q["symbol"], q.get("shortName", q["symbol"]), "当日涨幅榜靠前")
        for q in ranked[:_TOP_N_PER_SOURCE]
        if q.get("symbol")
    ]


def _most_active_candidates() -> list[tuple[str, str, str]]:
    """成交量榜：市场关注度/资金堆积的代理指标。"""
    quotes = _quotes("most_actives")
    ranked = sorted(quotes, key=lambda q: q.get("regularMarketVolume") or 0, reverse=True)
    return [
        (q["symbol"], q.get("shortName", q["symbol"]), "成交量榜靠前")
        for q in ranked[:_TOP_N_PER_SOURCE]
        if q.get("symbol")
    ]


def _most_shorted_candidates() -> list[tuple[str, str, str]]:
    """高做空比例榜：情绪/资金博弈维度，接近 A 股龙虎榜"游资博弈"的意味。"""
    quotes = _quotes("most_shorted_stocks")
    return [
        (q["symbol"], q.get("shortName", q["symbol"]), "做空比例靠前")
        for q in quotes[:_TOP_N_PER_SOURCE]
        if q.get("symbol")
    ]


def build_watchlist(limit: int) -> list[tuple[str, str, list[str]]]:
    """合并三路候选并去重；同时被多个来源命中的排在前面（跨维度确认度更高）。"""
    reasons: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}

    for candidates in (
        _day_gainers_candidates(),
        _most_active_candidates(),
        _most_shorted_candidates(),
    ):
        for symbol, name, reason in candidates:
            reasons[symbol].append(reason)
            names.setdefault(symbol, name)

    ranked = sorted(reasons.items(), key=lambda item: len(item[1]), reverse=True)
    return [(symbol, names[symbol], why) for symbol, why in ranked[:limit]]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=25, help="watchlist 最大只数，默认 25")
    parser.add_argument("--verbose", action="store_true", help="在 stderr 打印每只股票的入选原因")
    args = parser.parse_args()

    picks = build_watchlist(args.limit)
    if not picks:
        print("[build_watchlist_us] 没有筛出候选股，watchlist 为空", file=sys.stderr)
        return

    if args.verbose:
        print(f"[build_watchlist_us] 共 {len(picks)} 只：", file=sys.stderr)
        for symbol, name, why in picks:
            print(f"  {symbol} {name}  <- {', '.join(why)}", file=sys.stderr)

    print(",".join(symbol for symbol, _, _ in picks))


if __name__ == "__main__":
    main()
