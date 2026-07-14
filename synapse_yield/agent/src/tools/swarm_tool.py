"""SwarmTool: tool for the main agent to invoke a swarm multi-agent team.

The user provides a natural-language prompt; the tool auto-selects the best presets and extracts variables.
Blocks synchronously until the run completes and returns a JSON summary.

Trimmed from the original multi-presets src/tools/swarm_tool.py: this build
only bundles the news_technical_stock_picker presets, so _PRESET_KEYWORDS has
a single entry and the extractor helpers for variables no other presets here
needs (risk_tolerance, strategy_type, target_variable, review_period,
sector) were removed. The prompt -> presets -> variables routing pipeline
itself (_resolve_preset / _match_preset / _build_variables) is unchanged —
to add another presets back, add its keyword tuple to _PRESET_KEYWORDS and
its variable builder to _build_variables, same as before.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5
_MAX_WAIT_SECONDS = int(os.getenv("SWARM_TIMEOUT", "1800"))

# Preset matching: (preset_name, keyword_patterns, weight_boost). Patterns match user intent (EN + ZH).
_PRESET_KEYWORDS: list[tuple[str, list[str], float]] = [
    (
        "news_technical_stock_picker",
        [
            r"news.*technical",
            r"technical.*news",
            r"catalyst",
            r"stock\s+pick",
            r"stock\s+screen",
            "选股",
            "技术面",
            "新闻.*技术",
            "技术.*新闻",
            "催化剂",
        ],
        1.0,
    ),
]

# Market labels used in YAML templates (English, compatible with {market} placeholders).
_MARKET_PATTERNS: list[tuple[str, list[str]]] = [
    ("A-shares", [r"A股", r"a股", "沪深", "上证", "深证", "创业板", "科创板", "中证", r"\bCSI\b"]),
    ("crypto", ["加密", r"\bcrypto\b", r"\bBTC\b", r"\bETH\b", "币", "USDT", "数字货币"]),
    ("Hong Kong", ["港股", "恒生", r"H股", "港交所", r"\.HK\b"]),
    ("US", ["美股", "纳斯达克", "标普", "道琼斯", r"S&P", r"\.US\b"]),
]


def _extract_market(prompt: str) -> str:
    """Extract target market label from prompt.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Market label for template variables, default A-shares.
    """
    for market, patterns in _MARKET_PATTERNS:
        for pat in patterns:
            if re.search(pat, prompt, re.IGNORECASE):
                return market
    return "A-shares"


def _build_variables(preset_name: str, prompt: str) -> dict[str, str]:
    """Build template variables from prompt for the matched presets.

    Args:
        preset_name: Matched presets name.
        prompt: User's original prompt.

    Returns:
        Dict of template variables required by the YAML presets.
    """
    market = _extract_market(prompt)

    # Preset-specific variable sets (see agent/src/swarm/presets/*.yaml).
    builders: dict[str, dict[str, str]] = {
        "news_technical_stock_picker": {"market": market, "timeframe": "daily", "watchlist": "", "num_picks": "5"},
    }

    return builders.get(preset_name, {"market": market, "timeframe": "daily", "watchlist": "", "num_picks": "5"})


_PRESET_NAMES = {preset_name for preset_name, _, _ in _PRESET_KEYWORDS}
_CONTINUATION_PATTERNS = (
    r"^\s*continue\b",
    r"^\s*resume\b",
    r"^\s*finish\b",
    r"\bcontinue\s+(?:and\s+)?finish\b",
    r"\bcontinue\s+from\b",
    r"\bfinish\s+(?:the\s+)?report\b",
    r"\bcomplete\s+(?:the\s+)?report\b",
    r"\bpick\s+up\s+from\b",
    r"^\s*继续",
    r"^\s*接着",
)


def _match_preset(prompt: str) -> str:
    """Match user prompt to best presets using keyword scoring.

    Args:
        prompt: User's natural language prompt.

    Returns:
        Best matching presets name.
    """
    normalized_prompt = re.sub(r"[\s-]+", "_", prompt.strip().lower())
    for preset_name, _, _ in _PRESET_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(preset_name)}(?![a-z0-9])", normalized_prompt):
            return preset_name

    scores: dict[str, float] = {}
    for preset_name, keywords, boost in _PRESET_KEYWORDS:
        score = 0.0
        for kw in keywords:
            if re.search(kw, prompt, re.IGNORECASE):
                score += boost
        scores[preset_name] = score

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] > 0:
        return best

    return "news_technical_stock_picker"


def _normalize_preset_name(value: str) -> str | None:
    """Normalize an explicit presets name and validate it against bundled presets."""
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    return normalized if normalized in _PRESET_NAMES else None


def _has_preset_signal(prompt: str) -> bool:
    """Return whether prompt contains an explicit presets name or routing keyword."""
    normalized_prompt = re.sub(r"[\s-]+", "_", prompt.strip().lower())
    for preset_name, _, _ in _PRESET_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(preset_name)}(?![a-z0-9])", normalized_prompt):
            return True
    for _, keywords, _ in _PRESET_KEYWORDS:
        for kw in keywords:
            if re.search(kw, prompt, re.IGNORECASE):
                return True
    return False


def _looks_like_continuation_prompt(prompt: str) -> bool:
    """Detect prompts that refer to prior work instead of a fresh swarm task."""
    return any(re.search(pattern, prompt, re.IGNORECASE) for pattern in _CONTINUATION_PATTERNS)


def _resolve_preset(prompt: str, explicit_preset: str | None = None) -> tuple[str | None, str | None]:
    """Resolve the presets to run, returning an error string when ambiguous."""
    if explicit_preset:
        preset = _normalize_preset_name(explicit_preset)
        if preset is None:
            available = ", ".join(sorted(_PRESET_NAMES))
            return None, f"Unknown preset_name '{explicit_preset}'. Available presets: {available}"
        return preset, None

    if _looks_like_continuation_prompt(prompt) and not _has_preset_signal(prompt):
        return (
            None,
            "Ambiguous continuation swarm prompt. Reuse the previous swarm result, "
            "or call run_swarm with preset_name and the original full request. "
            "Refusing to auto-route this continuation to news_technical_stock_picker.",
        )

    return _match_preset(prompt), None


class SwarmTool(BaseTool):
    """Launch a swarm multi-agent team to execute complex tasks.

    Accepts a natural-language prompt, auto-selects the best presets,
    and blocks synchronously until the swarm run completes or times out.
    """

    name = "run_swarm"
    description = (
        "Run a multi-agent swarm team for complex analysis tasks. "
        "Provide a natural language prompt and, when known, an explicit preset_name from agent/src/swarm/presets "
        "(currently only news_technical_stock_picker is bundled) "
        "so follow-up/continuation prompts do not lose routing context. "
        "Example: run_swarm(prompt='Screen A-share stocks combining today's news catalysts with technical "
        "confirmation', preset_name='news_technical_stock_picker')"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Natural language description of the analysis task.",
            },
            "preset_name": {
                "type": "string",
                "description": "Optional explicit swarm presets name when the user named one or this is a continuation.",
            },
        },
        "required": ["prompt"],
    }
    is_readonly = False
    repeatable = True  # loop.py dedups by tool name; each prompt is a distinct run (#42)

    def __init__(
        self,
        *,
        include_shell_tools: bool = False,
        event_callback: Any | None = None,
    ) -> None:
        """Initialize the swarm launcher.

        Args:
            include_shell_tools: Whether worker registries may include shell
                execution tools requested by presets.
            event_callback: Optional session event bridge used by the web chat.
        """
        self.include_shell_tools = include_shell_tools
        self._event_callback = event_callback

    def _emit_session_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Forward swarm status to the hosting session SSE channel if present."""
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, data)
        except Exception:
            logger.warning("Failed to forward %s to session event stream", event_type, exc_info=True)

    def execute(self, **kwargs: Any) -> str:
        """Start a swarm run: auto-match presets, extract variables, wait for completion.

        Args:
            **kwargs: Must include prompt (str).

        Returns:
            JSON string with status, presets, variables, final_report, tasks, token_usage.
        """
        prompt = kwargs.get("prompt", "")

        if not prompt:
            return json.dumps(
                {"status": "error", "error": "Missing 'prompt' parameter"},
                ensure_ascii=False,
            )

        preset, preset_error = _resolve_preset(prompt, kwargs.get("preset_name"))
        if preset_error:
            return json.dumps(
                {"status": "error", "error": preset_error},
                ensure_ascii=False,
            )
        assert preset is not None
        variables = _build_variables(preset, prompt)

        logger.info(
            "SwarmTool: resolved presets=%s, variables=%s from prompt: %s",
            preset,
            variables,
            prompt[:100],
        )

        from src.config import load_swarm_agent_config
        from src.swarm.runtime import SwarmRuntime
        from src.swarm.store import SwarmStore

        swarm_base_dir = Path(__file__).resolve().parents[2] / ".swarm" / "runs"
        swarm_base_dir.mkdir(parents=True, exist_ok=True)
        store = SwarmStore(base_dir=swarm_base_dir)
        # Boot-time / operator-trusted: even when reached via the in-process
        # agent tool, the config path is resolved from disk / env, never from
        # the calling LLM's prompt (R-06).
        agent_config = load_swarm_agent_config()
        runtime = SwarmRuntime(
            store=store,
            max_workers=int(os.getenv("SWARM_MAX_WORKERS", "4")),
            agent_config=agent_config,
        )

        pending_live_events: list[dict[str, Any]] = []
        run_id_holder: dict[str, str | None] = {"run_id": None}

        try:
            def _live_callback(event: Any) -> None:
                payload = event.model_dump()
                current_run_id = run_id_holder["run_id"]
                if current_run_id is None:
                    pending_live_events.append(payload)
                    return
                self._emit_session_event(
                    "swarm.event",
                    {"run_id": current_run_id, "event": payload},
                )

            run = runtime.start_run(
                preset,
                variables,
                live_callback=_live_callback if self._event_callback is not None else None,
                include_shell_tools=self.include_shell_tools,
            )
        except FileNotFoundError as exc:
            return json.dumps(
                {"status": "error", "error": f"Preset not found: {exc}"},
                ensure_ascii=False,
            )
        except ValueError as exc:
            return json.dumps(
                {"status": "error", "error": f"Invalid DAG: {exc}"},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"status": "error", "error": f"Failed to start swarm: {exc}"},
                ensure_ascii=False,
            )

        run_id = run.id
        run_id_holder["run_id"] = run_id
        logger.info("SwarmTool: started run %s (presets=%s)", run_id, preset)
        self._emit_session_event(
            "swarm.started",
            {
                "run_id": run_id,
                "presets": preset,
                "variables": variables,
                "status": run.status.value,
                "agents": [agent.model_dump() for agent in run.agents],
                "tasks": [task.model_dump() for task in run.tasks],
            },
        )
        for event_payload in pending_live_events:
            self._emit_session_event(
                "swarm.event",
                {"run_id": run_id, "event": event_payload},
            )
        pending_live_events.clear()

        t0 = time.monotonic()
        while time.monotonic() - t0 < _MAX_WAIT_SECONDS:
            time.sleep(_POLL_INTERVAL_SECONDS)

            loaded = store.load_run(run_id)
            if loaded is None:
                return json.dumps(
                    {"status": "error", "error": f"Run {run_id} disappeared"},
                    ensure_ascii=False,
                )

            reconciled = store.reconcile_run(loaded, write=True)
            if reconciled.status.value in ("completed", "failed", "cancelled"):
                return _format_result(reconciled, preset, variables)

        # Wait budget elapsed but the run is still in flight. Do NOT cancel —
        # the daemon thread keeps working and the agent can decide to wait
        # more (re-invoke with the returned run_id) or hand off partial state
        # to the user. Cancelling here used to throw away minutes of LLM cost
        # whenever a presets legitimately ran past the budget.
        loaded = store.load_run(run_id)
        if loaded is not None:
            return _format_result(
                store.reconcile_run(loaded, write=True), preset, variables, timed_out=True
            )

        return json.dumps(
            {"status": "timeout", "error": f"Swarm run {run_id} timed out after {_MAX_WAIT_SECONDS}s"},
            ensure_ascii=False,
        )


def _format_result(
    run: Any,
    preset: str,
    variables: dict[str, str],
    timed_out: bool = False,
) -> str:
    """Format a SwarmRun into a JSON result string.

    Args:
        run: SwarmRun instance.
        preset: Matched presets name.
        variables: Extracted variables.
        timed_out: Whether the run was terminated due to timeout.

    Returns:
        JSON string with run status, report, task summaries, and token usage.
    """
    from src.swarm.serialization import run_level_error, serialize_task

    task_summaries = [serialize_task(task) for task in run.tasks]

    # ``timed_out`` only means the SwarmTool's wait budget elapsed — the run
    # itself is still progressing in the background. Surface the run's real
    # status so a downstream agent can re-invoke with the run_id (or end its
    # turn with a "still working" message) instead of treating it as failure.
    result = {
        "status": run.status.value,
        "wait_budget_exhausted": timed_out,
        "run_id": run.id,
        "presets": preset,
        "auto_variables": variables,
        "final_report": run.final_report or "",
        "error": run_level_error(run),
        "tasks": task_summaries,
        "token_usage": {
            "total_input_tokens": run.total_input_tokens,
            "total_output_tokens": run.total_output_tokens,
        },
    }
    return json.dumps(result, ensure_ascii=False, indent=2)
