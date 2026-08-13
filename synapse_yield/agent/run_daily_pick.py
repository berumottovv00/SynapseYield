

"""Entry point for the news+technical daily stock-picker swarm.

Usage:
    python synapse_yield/agent/run_daily_pick.py
    python synapse_yield/agent/run_daily_pick.py --market "A-shares" --num-picks 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import src.tools.swarm_tool as swarm_tool_module  # noqa: E402
from src.tools.swarm_tool import SwarmTool  # noqa: E402


def _patch_market_with_date(date: str) -> None:
    """Thread an as-of date into the {market} template slot.

    The preset has no native date variable — task templates hardcode
    "same-day news" — so _build_variables' market string is the only
    free-text slot that reaches the prompt templates.
    """
    original = swarm_tool_module._build_variables

    def patched(preset_name: str, prompt: str) -> dict[str, str]:
        variables = original(preset_name, prompt)
        variables["market"] = (
            f"{variables['market']} (treat {date} as the analysis date — scan news/catalysts "
            f"and price action as of {date}, NOT the actual current date)"
        )
        return variables

    swarm_tool_module._build_variables = patched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="A-shares", help="Target market/universe, e.g. A-shares / HK / US")
    parser.add_argument("--watchlist", default="", help="Comma-separated tickers to focus on (optional)")
    parser.add_argument("--num-picks", default="5", help="How many final picks to recommend")
    parser.add_argument("--date", default="", help="As-of date, e.g. 2026-07-10 (defaults to run date)")
    args = parser.parse_args()

    if args.date:
        _patch_market_with_date(args.date)

    prompt = (
        f"Screen {args.market} stocks combining today's news catalysts with technical confirmation. "
        f"Recommend {args.num_picks} picks."
        + (f" Focus on watchlist: {args.watchlist}." if args.watchlist else "")
    )

    tool = SwarmTool(include_shell_tools=True)
    result = tool.execute(prompt=prompt, preset_name="news_technical_stock_picker")
    print(json.dumps(json.loads(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()