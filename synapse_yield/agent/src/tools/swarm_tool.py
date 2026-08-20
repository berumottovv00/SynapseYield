"""SwarmTool：供主 Agent 调用 swarm 多智能体团队的工具。

这个模块是「单 Agent 交互循环」（agent/src/agent/loop.py）和「swarm 子系统」
（agent/src/swarm/）之间的桥梁。当顶层 Agent 判断某个任务需要一整个团队协作
（而不是自己单独处理）时——比如"结合今日新闻催化剂和技术面确认筛选 A 股"——
就会调用这个工具，而不是自己动手做。之后这个工具会：

  1. 决定要跑哪个 preset。preset 是 agent/src/swarm/presets/ 下的一个 YAML
     文件，定义了一个 Agent 团队 + 一组带依赖关系的任务 DAG。决定方式要么是
     显式的（调用方直接传 preset_name），要么是从自由文本 prompt 里按关键词
     推断出来的（_resolve_preset -> _match_preset）。
  2. 从 prompt 里提取模板变量（market、timeframe、watchlist、num_picks……），
     这些变量会被代入 preset 的 prompt 模板里（_build_variables）。
  3. 通过 SwarmRuntime.start_run() 启动运行——它会把运行/任务状态持久化到
     磁盘（SwarmStore），并启动工作线程，按 DAG 的依赖关系分别执行每个
     Agent 的任务。
  4. 每隔 _POLL_INTERVAL_SECONDS 秒轮询一次磁盘上的运行状态，直到运行进入
     终态（completed/failed/cancelled），或者等待预算耗尽
     （_MAX_WAIT_SECONDS，对应环境变量 SWARM_TIMEOUT）——哪个先到就算哪个。
  5. 把最终（或者还在跑的中间态）结果格式化成一段 JSON 字符串
     （_format_result）返回给调用方：状态、汇总出来的 final_report、每个
     任务的摘要、token 用量。

重要一点：就算这次调用的等待预算耗尽了，运行本身的后台工作线程**不会**被
取消，还会继续跑下去（见 execute() 末尾超时那个分支的注释）。调用方可以带着
同一个 run_id 再次调用这个工具去接着轮询，也可以直接不管它，让它自己在后台
跑完——不管哪种情况，都不会因为一次轮询窗口耗尽就白白扔掉已经花出去的 LLM
调用成本。

这一版是从更大的多 preset 版本 src/tools/swarm_tool.py 裁剪来的：这个 build
只捆绑了 news_technical_stock_picker 这一个 preset，所以 _PRESET_KEYWORDS
只有一条记录，其他 preset 专用的变量提取辅助函数（risk_tolerance、
strategy_type、target_variable、review_period、sector）都被删掉了。
「prompt -> preset -> variables」这条路由流水线本身（_resolve_preset /
_match_preset / _build_variables）没有变——以后要把别的 preset 加回来，
只需要往 _PRESET_KEYWORDS 里加一条关键词元组，再在 _build_variables 里
加一个对应的变量构造器，跟以前的做法一样。
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

# execute() 在等待期间每隔多久醒来重新检查一次磁盘上的运行状态。
_POLL_INTERVAL_SECONDS = 5
# execute() 总共愿意阻塞等待多久，超过这个时间就放弃继续等、直接把「还在跑」
# 的结果返回给调用方（见 execute() 末尾的超时分支）。可以用环境变量
# SWARM_TIMEOUT 配置。
_MAX_WAIT_SECONDS = int(os.getenv("SWARM_TIMEOUT", "1800"))

# Preset matching: (preset_name, keyword_patterns, weight_boost). Patterns match user intent (EN + ZH).
# 这个裁剪版只捆绑了一个 preset；_match_preset 会拿 prompt 跟这里每一条做
# 打分匹配，取分数最高的那个（具体算法见下面 _match_preset 的说明）。
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
# （市场标签, 命中该市场的正则模式列表）——按顺序遍历，哪个标签的模式列表
# 先在 prompt 里匹配上就用哪个，见下面的 _extract_market。
_MARKET_PATTERNS: list[tuple[str, list[str]]] = [
    ("A-shares", [r"A股", r"a股", "沪深", "上证", "深证", "创业板", "科创板", "中证", r"\bCSI\b"]),
    ("crypto", ["加密", r"\bcrypto\b", r"\bBTC\b", r"\bETH\b", "币", "USDT", "数字货币"]),
    ("Hong Kong", ["港股", "恒生", r"H股", "港交所", r"\.HK\b"]),
    ("US", ["美股", "纳斯达克", "标普", "道琼斯", r"S&P", r"\.US\b"]),
]


def _extract_market(prompt: str) -> str:
    """从 prompt 里猜用户说的是哪个市场，靠扫描 _MARKET_PATTERNS 来判断。

    按顺序遍历 (市场, 模式列表) 这些配对，返回第一个在 prompt 里匹配上的
    （大小写不敏感）市场标签。如果什么都没匹配上，退化成 "A-shares"——
    因为这个裁剪版唯一捆绑的 preset，它的 prompt 模板本来就是照着 A 股场景
    写的。

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
    """从自由文本 prompt 构造出某个 preset 的 YAML prompt 模板需要的模板变量。

    agent/src/swarm/presets/ 下每个 preset YAML 都声明了一个 `variables`
    区块（market、timeframe、watchlist、num_picks……），这些值会被代入每个
    Agent/任务的 prompt 模板里。这个函数就是具体去算出某次运行该用什么值的
    地方——目前只是识别出的市场（靠 _extract_market）加上一些固定默认值，
    因为目前唯一捆绑的这个 preset 不需要更复杂的东西。以后要把别的 preset
    加回来，就在下面的 `builders` 字典里给它加一条对应的记录。

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


# 由 _PRESET_KEYWORDS 派生出的「所有已捆绑 preset 名字」集合，用来校验调用方
# 传入的显式 preset_name 是否有效，也用来在 prompt 里直接点名某个 preset 时
# 短路跳过打分逻辑（见 _match_preset / _has_preset_signal）。
_PRESET_NAMES = {preset_name for preset_name, _, _ in _PRESET_KEYWORDS}

# 这些短语暗示用户是想「继续/恢复/完成之前那次 swarm 运行」，而不是在描述一个
# 全新的任务。_looks_like_continuation_prompt 用它来做识别，_resolve_preset
# 再拿这个识别结果去判断：要不要因为「怕把用户想接着跑的东西，误路由成一次
# 全新的（可能是错的）运行」而拒绝自动路由。
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
    """用关键词打分的方式，把 prompt 匹配到最合适的 preset。

    两阶段算法：
      1. 如果 prompt 里直接完整提到了某个已捆绑 preset 的名字（比如
         "……用 news_technical_stock_picker……"），那个 preset 直接胜出，
         不用再走打分流程。
      2. 否则，_PRESET_KEYWORDS 里的每个 preset 按其关键词列表打分——每有
         一条关键词模式在 prompt 里命中，就累加对应的 `weight_boost`，最后
         分数最高的 preset 胜出。
      3. 如果没有任何一个 preset 得分大于 0（一个关键词都没命中），就兜底
         返回唯一捆绑的那个 preset（"news_technical_stock_picker"），而不是
         返回空——反正这个裁剪版本来也只带了这一个 preset，没命中关键词的
         prompt 照样有地方可去。

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
    """规范化一个显式传入的 preset 名字，并校验它是否属于已捆绑的 preset。

    把值转小写，并把空白/连字符统一折叠成下划线（这样 "News Technical
    Stock-Picker" 和 "news_technical_stock_picker" 会被当成同一个 preset），
    然后拿处理结果去比对 _PRESET_NAMES。如果调用方指定的 preset 根本没有
    捆绑在这个 build 里，就返回 None，好让 _resolve_preset 能给出一个明确
    的报错，而不是悄悄地退回去走自动匹配。
    """
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    return normalized if normalized in _PRESET_NAMES else None


def _has_preset_signal(prompt: str) -> bool:
    """判断 prompt 里是否带有明确指向某个 preset 的信号（点名或关键词命中）。

    _resolve_preset 用它做一道保险：一个"看起来像是在续接之前的运行"的
    prompt（比如 "continue"、"接着……"）只有在**同时**完全没有任何 preset
    信号的情况下，才会被当成"有歧义"——如果它既像续接、又明确点名/暗示了
    某个 preset，那就没什么歧义可言，照常正常路由即可。
    """
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
    """识别出"这个 prompt 说的是接着之前的工作"，而不是一个全新的 swarm 任务。

    就是拿 _CONTINUATION_PATTERNS（"continue"、"resume"、"接着" 这些）做一次
    正则扫描。它本身并不知道"之前那次运行"是否真的存在——这个判断留给
    _resolve_preset，由它把这个结果和 _has_preset_signal 结合起来，决定要不
    要拒绝自动路由。
    """
    return any(re.search(pattern, prompt, re.IGNORECASE) for pattern in _CONTINUATION_PATTERNS)


def _resolve_preset(prompt: str, explicit_preset: str | None = None) -> tuple[str | None, str | None]:
    """决定这次要跑哪个 preset；如果情况模糊就返回一段错误说明。

    三分支决策：
      1. 调用方传了显式的 preset_name：拿 _normalize_preset_name 校验一下，
         通过就直接用；如果这个名字压根不在已捆绑的 preset 里，就返回一个
         列出所有可用 preset 的报错。
      2. 没有显式指定名字，但 prompt 读起来像"continue/resume/finish……"，
         并且完全没有别的信号能看出它指的是哪个 preset
         （_looks_like_continuation_prompt 为真且 _has_preset_signal 为假）：
         直接拒绝去猜。因为把一个有歧义的续接请求自动路由到打分最高的
         preset，有可能悄悄启动一次全新的（很可能是错的）运行，而不是接上
         用户真正想继续的那次——不如直接报错，让调用方要么复用上一次的
         swarm 结果，要么带着明确的 preset_name 重新调用。
      3. 除此之外：落到关键词自动匹配这条路（_match_preset）。

    Returns:
        (preset_name, None) on success, or (None, error_message) when the
        preset_name argument is invalid or the prompt is an ambiguous
        continuation.
    """
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
    """启动一个 swarm 多智能体团队来执行复杂任务。

    接收一段自由文本 prompt，自动选出最合适的 preset，然后同步阻塞等待，
    直到这次 swarm 运行完成或者超时。

    这是宿主 Agent 唯一会调用的入口；这个模块里其余的内容（preset 解析、
    变量提取、结果格式化）都是为了支撑下面的 execute() 而存在的。真正的多
    智能体执行逻辑——启动 worker、跑 DAG、给失败的 LLM 调用做重试、持久化
    状态——都在 agent/src/swarm/runtime.py 里，这里只是负责编排（启动、
    轮询、汇总结果）。
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
        """初始化这个 swarm 启动器。

        Args:
            include_shell_tools: Whether worker registries may include shell
                execution tools requested by presets.
            event_callback: Optional session event bridge used by the web chat.
        """
        self.include_shell_tools = include_shell_tools
        self._event_callback = event_callback

    def _emit_session_event(self, event_type: str, data: dict[str, Any]) -> None:
        """如果存在会话事件回调，就把 swarm 状态转发过去（供 Web 聊天用）。

        没传 event_callback 的时候（比如 run_daily_pick.py 那种纯命令行路径）
        什么都不做，直接返回——这个机制只在 SwarmTool 是被 Web 聊天端驱动时
        才有意义，因为那边想把实时进度推给浏览器，而不是傻等 execute() 跑完
        才返回结果。回调本身抛出的异常会被吞掉（只记一条警告日志），这样一个
        不稳定的 UI 推送通道永远不会拖垮真正的 swarm 运行。
        """
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, data)
        except Exception:
            logger.warning("Failed to forward %s to session event stream", event_type, exc_info=True)

    def execute(self, **kwargs: Any) -> str:
        """启动一次 swarm 运行：自动匹配 preset、提取变量、等待运行结束。

        端到端流程：
          1. 校验 prompt 参数确实存在。
          2. 解析出要跑哪个 preset（_resolve_preset），并算出它的模板变量
             （_build_variables）；这两步任一失败就直接返回 JSON 格式的报错。
          3. 构造一个 SwarmRuntime（背后由 SwarmStore 负责把运行/任务状态
             持久化到 agent/.swarm/runs/ 目录下），调用 start_run()——它会
             启动真正干活的工作线程，并立刻返回一个全新的 run_id。
          4. 每隔 _POLL_INTERVAL_SECONDS 秒轮询一次磁盘上的运行状态
             （store.reconcile_run），直到进入终态，或者 _MAX_WAIT_SECONDS
             这个等待预算耗尽。
          5. 把最终（或者还在跑的中间态）状态，通过 _format_result 格式化成
             一段 JSON 字符串返回。

        runtime 在 start_run() 返回之前（也就是 run_id 还不知道的时候）就可能
        已经触发了一些实时进度事件——这些事件会先缓存进
        pending_live_events，等 run_id 到手之后再统一补发上去，这样能补上
        一个很小的时间窗口漏洞：不然最开始那几个事件会没地方可发。

        超时的时候，这次运行**不会**被取消（详见下面靠近末尾那段注释）——
        不管这次调用最终返回了什么，它的工作线程都会在后台继续跑下去。

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

        # run_id 要等下面 start_run() 返回之后才知道，但 runtime 可能在这之前
        # 就已经触发了实时事件——先缓存在这，等 start_run() 跑完之后再统一
        # 补发（带上 run_id）。
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

        # 运行已经启动；解除上面缓存事件那条路径的阻塞，把 start_run() 启动
        # worker 到它真正返回这段时间里攒下的事件一并冲刷出去。
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

        # 轮询循环：每隔 _POLL_INTERVAL_SECONDS 秒重新读一次持久化的运行
        # 状态，一旦进入终态（completed/failed/cancelled）就立刻返回；否则
        # 一直等到 _MAX_WAIT_SECONDS 耗尽为止——哪个先到就按哪个算。
        # reconcile_run 顺带还会做一次"恢复/清理僵死运行"的检查（见
        # SwarmStore），write=True 表示这类恢复动作要落盘持久化。
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
    """把一个 SwarmRun 对象格式化成 JSON 结果字符串。

    把调用方（不管是上层 Agent 还是像 run_daily_pick.py 这样的命令行脚本）
    想知道的关于这次运行的一切一次性打包好：终态（或中间态）状态、方便重新
    轮询用的 run_id、跑的是哪个 preset 以及自动识别出的变量、汇总出来的
    final_report 正文、按任务拆分的明细（靠 serialize_task）、顶层错误信息
    （如果有的话），以及这次运行里所有 worker LLM 调用累计的 token 用量。

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

    # ``timed_out`` 只表示 SwarmTool 这次调用的等待预算用完了——运行本身还在
    # 后台继续推进。这里要把运行的真实状态如实反映出来，好让下游 Agent 可以
    # 选择带着 run_id 重新调用继续等（或者先跟用户说一句"还在跑"），而不是
    # 把这次超时误当成运行失败。
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
