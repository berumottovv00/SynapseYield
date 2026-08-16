#!/usr/bin/env python3
"""挑选当日候选股 watchlist，供 run_daily_pick.py --watchlist 使用。

不调用 LLM：直接用 akshare（免 token，爬东方财富公开页面）拉「今日涨停」
「龙虎榜」「个股资金流向」三类数据做机械筛选，把「发现候选股」这一步从
news_analyst / technical_screener 的 ReAct 迭代里挪出来，交给确定性代码做，
从而减少 swarm 运行所需的迭代数和耗时。

akshare 没有官方限速保证，个别接口偶尔会被源站限流/断连——每个数据源都单独
try/except，某一路失败只是少一路候选，不会导致整个脚本失败。

用法：
    python synapse_yield/agent/build_watchlist.py
    python synapse_yield/agent/build_watchlist.py --date 20260810 --limit 20 --verbose

    # 直接接到 run_daily_pick.py：
    python synapse_yield/agent/run_daily_pick.py \
        --watchlist "$(python synapse_yield/agent/build_watchlist.py)"
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date

import akshare as ak
import requests

_TOP_N_PER_SOURCE = 15  # 每类数据各取多少只候选，供后续合并去重

_FUND_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_FUND_FLOW_PARAMS = {
    "fid": "f62",  # 今日主力净流入-净额，服务端已按此字段降序返回
    "po": "1",
    "pz": "100",  # 只取第一页（已排序），够覆盖 _TOP_N_PER_SOURCE
    "pn": "1",
    "np": "1",
    "fltt": "2",
    "invt": "2",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
    "fs": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2",
    "fields": "f12,f14,f62",
}


def _latest_trade_date(as_of: str) -> str:
    """返回不晚于 as_of（YYYYMMDD）的最近一个 A 股交易日（YYYYMMDD）。"""
    cal = ak.tool_trade_date_hist_sina()
    as_of_date = date(int(as_of[:4]), int(as_of[4:6]), int(as_of[6:]))
    valid = cal[cal["trade_date"] <= as_of_date]
    if valid.empty:
        raise SystemExit(f"找不到不晚于 {as_of} 的交易日")
    return valid["trade_date"].max().strftime("%Y%m%d")


def _warn(source: str, exc: Exception) -> None:
    print(f"[build_watchlist] {source} 拉取失败，跳过（{exc!r}）", file=sys.stderr)


def _limit_up_candidates(trade_date: str) -> list[tuple[str, str, str]]:
    """今日涨停股：按封板资金（资金抢筹力度）排序。"""
    try:
        df = ak.stock_zt_pool_em(date=trade_date)
    except Exception as exc:
        _warn("涨停股池", exc)
        return []
    if df.empty:
        return []
    df = df.sort_values("封板资金", ascending=False).head(_TOP_N_PER_SOURCE)
    return [(row.代码, row.名称, "涨停封单靠前") for row in df.itertuples()]


def _dragon_tiger_candidates(trade_date: str) -> list[tuple[str, str, str]]:
    """龙虎榜：按净买入额排序，只取净买入为正的。"""
    try:
        df = ak.stock_lhb_detail_em(start_date=trade_date, end_date=trade_date)
    except Exception as exc:
        _warn("龙虎榜", exc)
        return []
    if df.empty:
        return []
    # 同一只股票可能因多个「上榜原因」出现多行，按净买额排序后去重，避免重复计入权重。
    df = (
        df[df["龙虎榜净买额"] > 0]
        .sort_values("龙虎榜净买额", ascending=False)
        .drop_duplicates(subset="代码")
        .head(_TOP_N_PER_SOURCE)
    )
    return [(row.代码, row.名称, "龙虎榜净买入靠前") for row in df.itertuples()]


def _fund_flow_candidates() -> list[tuple[str, str, str]]:
    """个股资金流向：东方财富今日主力净流入排名第一页，只取净流入为正的。

    不用 akshare 自带的 stock_individual_fund_flow_rank——它会翻遍全市场几十页，
    这里只请求已排序好的第一页（前 100），一次请求即可，减少被限流的概率。
    """
    try:
        resp = requests.get(_FUND_FLOW_URL, params=_FUND_FLOW_PARAMS, timeout=15)
        resp.raise_for_status()
        rows = resp.json()["data"]["diff"]
    except Exception as exc:
        _warn("个股资金流向", exc)
        return []
    candidates = [
        (row["f12"], row["f14"], "主力资金净流入靠前")
        for row in rows
        if isinstance(row.get("f62"), (int, float)) and row["f62"] > 0
    ]
    return candidates[:_TOP_N_PER_SOURCE]


def build_watchlist(trade_date: str, limit: int) -> list[tuple[str, str, list[str]]]:
    """合并三路候选并去重；同时被多个来源命中的排在前面（跨维度确认度更高）。"""
    reasons: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}

    sources = [
        _limit_up_candidates(trade_date),
        _dragon_tiger_candidates(trade_date),
        _fund_flow_candidates(),
    ]
    for candidates in sources:
        for code, name, reason in candidates:
            reasons[code].append(reason)
            names.setdefault(code, name)

    ranked = sorted(reasons.items(), key=lambda item: len(item[1]), reverse=True)
    return [(code, names[code], why) for code, why in ranked[:limit]]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--date", default="", help="截止日期 YYYYMMDD/YYYY-MM-DD，默认今天")
    parser.add_argument("--limit", type=int, default=25, help="watchlist 最大只数，默认 25")
    parser.add_argument("--verbose", action="store_true", help="在 stderr 打印每只股票的入选原因")
    args = parser.parse_args()

    as_of = args.date.replace("-", "") if args.date else date.today().strftime("%Y%m%d")
    trade_date = _latest_trade_date(as_of)

    picks = build_watchlist(trade_date, args.limit)
    if not picks:
        print(f"[build_watchlist] {trade_date} 没有筛出候选股，watchlist 为空", file=sys.stderr)
        return

    if args.verbose:
        print(f"[build_watchlist] 交易日 {trade_date}，共 {len(picks)} 只：", file=sys.stderr)
        for code, name, why in picks:
            print(f"  {code} {name}  <- {', '.join(why)}", file=sys.stderr)

    print(",".join(code for code, _, _ in picks))


if __name__ == "__main__":
    main()
