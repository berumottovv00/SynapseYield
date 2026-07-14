"""Swarm DAG orchestration runtime.

Core orchestrator: schedules workers by topological layer, parallel within each
layer and serial between layers. Execution runs in a background daemon thread
with cancellation and event callback support.

中文说明
--------
本文件是 swarm(多智能体协作)模块的核心调度引擎 —— ``SwarmRuntime``。

一次 swarm 运行(SwarmRun)由若干个任务(SwarmTask)组成，任务之间可以有依赖关系
（``depends_on``），整体构成一张有向无环图（DAG，见 task_store.py 里的
``topological_layers`` / ``validate_dag``）。调度模型是"分层执行"：
    - 先把所有任务按依赖关系分层（同一层内的任务互相之间没有依赖）；
    - 同一层内的多个任务通过线程池 **并行** 执行（每个任务对应一个 worker，
      即一次独立的、带工具调用能力的 ReAct 循环，具体实现见 ``worker.py``）；
    - 层与层之间 **串行** 执行，必须等上一层全部完成/失败/被跳过后，
      才会进入下一层（这样下游任务才能拿到上游任务的产出摘要作为上下文）。

每次调用 ``start_run`` 都会：
    1. 立即返回一个状态为 pending 的 SwarmRun（不阻塞调用方）；
    2. 在一个新的**后台守护线程**（daemon thread）里真正执行 ``_execute_run``，
       这样即使宿主进程退出，这些线程也不会阻止进程退出。

运行状态通过 ``SwarmStore``/``TaskStore`` 落盘到
``.swarm/runs/{run_id}/`` 目录（run.json、tasks/*.json、events.jsonl），
所以即使调用方（比如前端页面）刷新或断开，也能通过读盘拿到最新进度。
实时进度则通过 ``_emit_event`` 双写：一份写入 events.jsonl 做持久化，
一份通过 ``live_callback`` 回调转发给调用方（例如 API 层的 SSE 推流）。

此外还支持"取消"（``cancel_run``）：内部用一个 ``threading.Event`` 在层与层的
边界处轮询，一旦检测到取消信号，就把当前层还没跑完的任务标记为 cancelled，
不再进入下一层。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.config.schema import AgentConfig
from src.swarm import grounding
from src.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmEvent,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
)
from src.swarm.presets import build_run_from_preset
from src.swarm.store import SwarmStore
from src.swarm.task_store import (
    TaskStore,
    resolve_dependencies,
    topological_layers,
    validate_dag,
)
from src.tools.redaction import redact_internal_paths
from src.swarm.worker import run_worker

logger = logging.getLogger(__name__)


class SwarmRuntime:
    """Swarm DAG orchestration engine.

    Manages the full lifecycle of a swarm run: creation, scheduling, execution,
    and cancellation. Each run executes in an independent background daemon thread;
    tasks within a layer run in parallel via ThreadPoolExecutor.

    Attributes:
        _store: SwarmStore persistence layer.
        _max_workers: Maximum concurrent workers in ThreadPoolExecutor.
    """

    def __init__(
        self,
        store: SwarmStore,
        max_workers: int = 4,
        agent_config: AgentConfig | None = None,
    ) -> None:
        """Initialize SwarmRuntime.

        Args:
            store: SwarmStore instance for run persistence.
            max_workers: Maximum concurrent worker threads.
            agent_config: Optional resolved agent config carrying remote MCP
                server definitions. Boot-time / operator-trusted; never derived
                from a swarm caller. Forwarded to every worker on every run so
                the worker can assemble a registry that includes remote MCP
                tools. ``None`` (the default) preserves the current
                local-tool-only behavior byte-for-byte.
        """
        self._store = store
        self._max_workers = max_workers
        self._agent_config = agent_config
        # run_id -> 取消信号。每个 run 一个独立的 Event，_execute_run 的
        # 后台线程会在层与层的边界处检查它，检测到 set() 就中止后续层的调度。
        self._cancel_events: dict[str, threading.Event] = {}
        # run_id -> 实时回调。用于把 SwarmEvent 实时转发给调用方（例如 API
        # 层用它把事件通过 SSE 推给前端），与落盘到 events.jsonl 是两条独立路径。
        self._live_callbacks: dict[str, Callable] = {}
        # 保护上面两个字典的并发访问：_cancel_events/_live_callbacks 会被
        # 多个 run 的后台线程 + 发起取消请求的主线程同时读写。
        self._lock = threading.Lock()

    def start_run(
        self,
        preset_name: str,
        user_vars: dict[str, str],
        live_callback: Callable | None = None,
        include_shell_tools: bool = False,
    ) -> SwarmRun:
        """Start a swarm run. Returns immediately, execution happens in background.

        Args:
            preset_name: YAML presets name to execute.
            user_vars: User-provided variables for prompt templates.
            live_callback: Optional callback invoked for each event in real-time.
            include_shell_tools: Whether workers may register shell tools.

        Returns:
            The created SwarmRun instance (status=pending initially).

        Raises:
            FileNotFoundError: If presets does not exist.
            ValueError: If DAG validation fails.
        """
        # Reap any previously running runs whose host process died without
        # finalizing them. Threshold is computed per-run from agent timeouts +
        # heartbeat interval (see SwarmStore.compute_stale_threshold), so a
        # legitimately slow long-running task is not killed.
        try:
            # 调用存储层的方法，去扫描磁盘上所有状态是 running
            # 的记录，判断哪些"看起来早就该结束了但一直没结束"（上面注释提到，判断阈值是"按每个 run 的 agent 超时时间 +
            # 心跳间隔动态算出来的"，不是写死的固定时间，这样能避免把真正在跑的慢任务误杀）。对判定为僵尸的记录，把它们标记为某种终态（比如 failed），并返回被处理的 run id 列表 reaped。
            reaped = self._store.reap_stale_running_runs()
            if reaped:
                logger.info("Reaped %d stale swarm run(s): %s", len(reaped), reaped)
        except Exception:
            logger.warning("Stale-run reaper failed", exc_info=True)

        # 把 YAML 预设（agents + tasks 模板）渲染成本次运行的具体 SwarmRun：
        # user_vars 会替换任务 prompt 里的模板变量，同时生成唯一的 run.id。
        run = build_run_from_preset(preset_name, user_vars)
        # 校验任务依赖图：不能有环，depends_on 指向的任务必须存在。
        # 校验失败直接抛异常，run 根本不会被创建/落盘。
        validate_dag(run.tasks)

        # Capture which provider/model the run was launched against so the
        # serialized run.json carries enough context for cost audits and
        # post-hoc debugging. Read directly from the same env vars the
        # provider layer uses (src/providers/llm.py:136,195) — that way an
        # override applied via os.environ still shows up. Per-agent overrides
        # remain visible on SwarmAgentSpec.model_name.
        run.provider = (os.getenv("LANGCHAIN_PROVIDER") or "").strip().lower() or None
        run.model = (os.getenv("LANGCHAIN_MODEL_NAME") or "").strip() or None

        self._store.create_run(run)

        # 注册取消信号 + 实时回调，供后台线程和 cancel_run() 共用。
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[run.id] = cancel_event
            if live_callback is not None:
                self._live_callbacks[run.id] = live_callback

        # 真正的调度/执行放到一个后台守护线程里跑，daemon=True 保证它不会
        # 阻止进程退出。start_run() 本身立刻返回，调用方（API/CLI）拿到
        # run（此时 status=pending）之后就可以轮询/订阅事件来看进度。
        thread = threading.Thread(
            target=self._execute_run,
            args=(run, cancel_event, include_shell_tools),
            name=f"swarm-{run.id}",
            daemon=True,
        )
        thread.start()

        return run

    def cancel_run(self, run_id: str) -> bool:
        """Signal cancellation for a running swarm.

        Args:
            run_id: ID of the run to cancel.

        Returns:
            True if cancellation was signalled, False if run not found.
        """
        with self._lock:
            cancel_event = self._cancel_events.get(run_id)
        if cancel_event is None:
            return False
        # 只是把 Event 置位，不会立刻打断正在跑的 worker —— 真正生效的时机是
        # _execute_run 在下一个"层边界"检查到这个信号时（同一层内已经在跑
        # 的任务会自然跑完，不会被强杀）。
        cancel_event.set()
        return True

    def _emit_event(self, run_id: str, event: SwarmEvent) -> None:
        """Persist an event and forward to live callback if registered.

        Args:
            run_id: Run identifier.
            event: Event to persist.
        """
        try:
            self._store.append_event(run_id, event)
        except Exception:
            logger.warning("Failed to persist event for run %s", run_id, exc_info=True)
        with self._lock:
            cb = self._live_callbacks.get(run_id)
        if cb is not None:
            try:
                cb(event)
            except Exception:
                logger.warning("Live callback failed for run %s", run_id, exc_info=True)

    def _make_event(
        self,
        event_type: str,
        agent_id: str | None = None,
        task_id: str | None = None,
        data: dict | None = None,
    ) -> SwarmEvent:
        """Create a SwarmEvent with current timestamp.

        Args:
            event_type: Event type string.
            agent_id: Optional agent identifier.
            task_id: Optional task identifier.
            data: Optional additional data.

        Returns:
            SwarmEvent instance.
        """
        return SwarmEvent(
            type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            data=data or {},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _execute_run(
        self,
        run: SwarmRun,
        cancel_event: threading.Event,
        include_shell_tools: bool = False,
    ) -> None:
        """Core orchestration loop (runs in background thread).

        Steps:
            1. Update run status to running
            2. Initialize TaskStore, save all tasks
            3. Compute topological layers
            4. For each layer:
               a. Check cancellation
               b. Submit all tasks to ThreadPoolExecutor
               c. Collect results, resolve dependencies, update store
            5. Update run status to completed/failed

        Args:
            run: SwarmRun to execute.
            cancel_event: Threading event for cancellation signalling.
            include_shell_tools: Whether workers may register shell tools.
        """
        run_id = run.id
        run_dir = self._store.run_dir(run_id)

        # Mark as running
        run.status = RunStatus.running
        self._store.update_run(run)
        self._emit_event(run_id, self._make_event("run_started"))

        # 提前抓取 user_vars 里出现的标的（股票代码等）的真实行情数据，
        # 挂到 run.grounding_data 上，避免 LLM worker 凭训练记忆瞎编价格。
        self._prefetch_grounding_data(run)

        # 把这次 run 的所有任务落盘为独立的 tasks/*.json 文件（TaskStore），
        # 后续每个任务的状态更新都是对这些文件的原子写入，是任务状态的
        # "唯一真相来源"；run.json 只是层边界处同步的一份粗粒度快照。
        task_store = TaskStore(run_dir)
        for task in run.tasks:
            task_store.save_task(task)

        # agent_id -> SwarmAgentSpec，方便按任务的 agent_id 查角色配置
        # （模型、超时时间、最大重试次数、工具集等）。
        agent_map: dict[str, SwarmAgentSpec] = {a.id: a for a in run.agents}

        # Render the grounding block once and pass it to every worker on
        # this run. The block is empty when no symbols were detected, in
        # which case workers see no extra section.
        grounding_block = grounding.format_grounding_block(run.grounding_data or {})

        # 依据 depends_on 关系把任务分层：同一层内互不依赖，可以安全并行；
        # 层的顺序即执行顺序（第 0 层最先跑，最后一层最后跑）。
        layers = topological_layers(run.tasks)
        # task_id -> 该任务完成后的摘要文本，供下游任务作为上游上下文使用
        # （见 _execute_layer 里的 upstream 变量）。
        task_summaries: dict[str, str] = {}
        all_succeeded = True

        try:
            for layer_idx, layer_task_ids in enumerate(layers):
                # 取消检查只在"层边界"做：本层任务不会被中途打断，
                # 但确认取消后就不再调度后续层。
                if cancel_event.is_set():
                    logger.info("Run %s cancelled at layer %d", run_id, layer_idx)
                    self._cancel_remaining_tasks(task_store, layer_task_ids, run.tasks)
                    all_succeeded = False
                    break

                self._emit_event(
                    run_id,
                    self._make_event(
                        "layer_started",
                        data={"layer": layer_idx, "tasks": layer_task_ids},
                    ),
                )

                # 并行跑完这一层的所有任务（内部用线程池 + 逐任务重试），
                # 阻塞到这一层全部任务都有结果（或触发整层超时）才返回。
                layer_results = self._execute_layer(
                    run=run,
                    task_store=task_store,
                    agent_map=agent_map,
                    layer_task_ids=layer_task_ids,
                    task_summaries=task_summaries,
                    run_dir=run_dir,
                    cancel_event=cancel_event,
                    include_shell_tools=include_shell_tools,
                    grounding_block=grounding_block,
                )

                # 逐个处理本层每个任务的结果，把 token 消耗累加到 run 级别统计，
                # 并把每个任务的最终状态（completed/failed）写回 task_store。
                for tid, result in layer_results.items():
                    # Accumulate token counts to run totals
                    run.total_input_tokens += result.input_tokens
                    run.total_output_tokens += result.output_tokens

                    if result.status == "completed":
                        # 摘要存进 task_summaries，下一层任务如果 depends_on
                        # 这个任务，就能在 _execute_layer 里把它拼进自己的
                        # upstream 上下文（而不是把上游的完整输出都塞进去）。
                        task_summaries[tid] = result.summary
                        now_iso = datetime.now(timezone.utc).isoformat()
                        task_store.update_status(
                            tid,
                            TaskStatus.completed,
                            summary=result.summary,
                            completed_at=now_iso,
                            artifacts=result.artifact_paths,
                            worker_iterations=result.iterations,
                        )
                        # 把依赖这个任务的下游任务的 depends_on 计数标记为
                        # "已满足一个"，供后续层判断是否所有前置都完成了。
                        resolve_dependencies(run_dir / "tasks", tid)
                        self._emit_event(
                            run_id,
                            self._make_event(
                                "task_completed",
                                task_id=tid,
                                data={
                                    "status": result.status,
                                    "iterations": result.iterations,
                                    "input_tokens": result.input_tokens,
                                    "output_tokens": result.output_tokens,
                                },
                            ),
                        )
                    else:
                        all_succeeded = False
                        task_store.update_status(
                            tid,
                            TaskStatus.failed,
                            error=redact_internal_paths(result.error)
                            or f"worker did not complete (status={result.status})",
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            worker_iterations=result.iterations,
                        )
                        self._emit_event(
                            run_id,
                            self._make_event(
                                "task_failed",
                                task_id=tid,
                                data={
                                    "error": redact_internal_paths(result.error),
                                    "input_tokens": result.input_tokens,
                                    "output_tokens": result.output_tokens,
                                },
                            ),
                        )

                # Tasks blocked by a failed upstream are never dispatched and
                # therefore not present in layer_results — they were already
                # marked TaskStatus.blocked in _execute_layer and emitted
                # task_blocked. Account for them in run-level status so the
                # run is marked failed, not silently completed.
                for tid in layer_task_ids:
                    if tid not in layer_results:
                        all_succeeded = False

                # Snapshot run.json at the layer boundary so list_runs and any
                # client that reads run.json directly sees fresh task statuses
                # without per-task I/O spam. One write per layer is cheap.
                self._sync_run_tasks_snapshot(run, task_store)

        except Exception as exc:
            logger.error("Run %s failed with exception", run_id, exc_info=True)
            all_succeeded = False
            self._emit_event(
                run_id,
                self._make_event("run_error", data={"error": redact_internal_paths(str(exc))}),
            )

        # 汇总最终状态：取消优先于失败，失败优先于成功
        # （即只要有一个任务失败/被阻塞，整个 run 就不算 completed）。
        final_status = (
            RunStatus.cancelled if cancel_event.is_set() else RunStatus.completed if all_succeeded else RunStatus.failed
        )
        run.status = final_status
        run.completed_at = datetime.now(timezone.utc).isoformat()

        # Sync tasks back to run model
        run.tasks = task_store.load_all()

        # 把最后一层里第一个有摘要的任务的输出当作整个 run 的"最终报告"
        # ——预设 YAML 通常把汇总/决策类任务放在最后一层（比如
        # investment_committee 预设里的 portfolio_manager）。
        if task_summaries:
            last_layer = layers[-1] if layers else []
            for tid in last_layer:
                if tid in task_summaries:
                    run.final_report = task_summaries[tid]
                    break

        self._store.update_run(run)
        self._emit_event(run_id, self._make_event("run_completed", data={"status": final_status.value}))

        # Cleanup cancel event and live callback
        with self._lock:
            self._cancel_events.pop(run_id, None)
            self._live_callbacks.pop(run_id, None)

    def _sync_run_tasks_snapshot(self, run: SwarmRun, task_store: TaskStore) -> None:
        """Mirror live ``tasks/*.json`` back into ``run.json`` at a safe point.

        Called at layer boundaries only — not per-task — to keep run.json a
        useful coarse snapshot for ``list_runs`` and CLI/Web callers that
        don't hydrate per request. Failures are logged but never fatal: the
        per-task files are still the live source of truth.
        """
        try:
            run.tasks = task_store.load_all()
            self._store.update_run(run)
        except Exception:
            logger.warning("Layer-boundary run.json sync failed", exc_info=True)

    def _prefetch_grounding_data(self, run: SwarmRun) -> None:
        """Fetch run-level grounding data without blocking ``start_run``."""
        symbols = grounding.extract_symbols_from_user_vars(run.user_vars)
        if not symbols:
            return

        symbol_limit = grounding.max_grounding_symbols()
        if len(symbols) > symbol_limit:
            logger.warning(
                "grounding: limiting run %s symbols from %d to %d",
                run.id,
                len(symbols),
                symbol_limit,
            )
            symbols = symbols[:symbol_limit]

        # Multi-symbol grounding fetch can take 30s+ on slow loaders. Wrap it
        # in a heartbeat so events.jsonl gets fresh entries during the fetch
        # — without this, the stale-run reaper would false-positive-mark a
        # healthy fresh run that's just waiting on OHLCV API calls.
        from src.agent.progress import HeartbeatTimer

        def _on_grounding_heartbeat(payload: dict) -> None:
            self._emit_event(
                run.id,
                self._make_event(
                    "run_heartbeat",
                    data={**payload, "phase": "grounding"},
                ),
            )

        try:
            interval = float(os.getenv("SWARM_HEARTBEAT_INTERVAL_S", "3.0"))
        except ValueError:
            interval = 3.0

        try:
            with HeartbeatTimer(
                tool_name=f"grounding:{len(symbols)}symbols",
                interval=interval,
                emit=_on_grounding_heartbeat,
            ):
                fetched = grounding.fetch_grounding_data(symbols)
        except Exception:
            logger.warning(
                "grounding: pre-fetch failed for run %s symbols=%s",
                run.id,
                symbols,
                exc_info=True,
            )
            return

        if fetched:
            run.grounding_data = fetched
            self._store.update_run(run)

    def _execute_layer(
        self,
        run: SwarmRun,
        task_store: TaskStore,
        agent_map: dict[str, SwarmAgentSpec],
        layer_task_ids: list[str],
        task_summaries: dict[str, str],
        run_dir: Path,
        cancel_event: threading.Event,
        include_shell_tools: bool = False,
        grounding_block: str = "",
    ) -> dict[str, WorkerResult]:
        """Execute all tasks in a single layer in parallel, with retry on failure.

        Each task is retried up to agent_spec.max_retries times if the worker
        returns status="failed". A "task_retry" event is emitted before each retry.

        Args:
            run: The SwarmRun being executed.
            task_store: TaskStore for task persistence.
            agent_map: Agent specs keyed by agent_id.
            layer_task_ids: Task IDs in this layer.
            task_summaries: Accumulated task summaries from previous layers.
            run_dir: Run directory path.
            cancel_event: Cancellation event.
            include_shell_tools: Whether workers may register shell tools.
            grounding_block: Pre-rendered "Ground Truth" markdown for workers.

        Returns:
            Mapping of task_id -> WorkerResult for all tasks in this layer.
        """
        results: dict[str, WorkerResult] = {}

        def _event_callback(event: SwarmEvent) -> None:
            self._emit_event(run.id, event)

        # Manual executor lifecycle (not `with`) so KeyboardInterrupt and
        # the layer deadline don't block main on `shutdown(wait=True)` —
        # `wait=False + cancel_futures=True` lets pending work drop and
        # the CLI return immediately. Running workers finish naturally.
        executor = ThreadPoolExecutor(max_workers=self._max_workers)
        futures: dict[Future[WorkerResult], str] = {}
        layer_budget = 0  # seconds — max per-task (retries × timeout) across layer
        try:
            # 逐个任务判断能否派发：先做依赖门控检查，通过了才真正提交给线程池。
            # 注意这里是"同层内串行地判断 + 提交"，但判断本身很快（读几个小
            # JSON 文件），真正耗时的 worker 执行是异步并行跑的。
            for tid in layer_task_ids:
                task = task_store.load_task(tid)

                # Dependency-aware gating: without this check, a failed upstream
                # silently produces an empty task_summaries entry (the worker
                # upstream loop below only copies summaries that exist) and the
                # downstream worker runs with no upstream context. For an
                # investment-committee presets where portfolio_manager
                # depends_on=["task-risk"], a failed risk_officer would let PM
                # produce a "decision" with no risk input — which is
                # safety-critical. Mark blocked and skip dispatch; same-layer
                # peers with no shared upstream are unaffected.
                blocked_upstreams: list[tuple[str, str]] = []
                for dep_id in task.depends_on:
                    try:
                        dep_task = task_store.load_task(dep_id)
                    except FileNotFoundError:
                        blocked_upstreams.append((dep_id, "missing"))
                        continue
                    if dep_task.status != TaskStatus.completed:
                        blocked_upstreams.append((dep_id, dep_task.status.value))

                if blocked_upstreams:
                    reason = ", ".join(f"{d}={s}" for d, s in blocked_upstreams)
                    blocked_by_ids = [d for d, _ in blocked_upstreams]
                    task_store.update_status(
                        tid,
                        TaskStatus.blocked,
                        error=f"Blocked: upstream not completed ({reason})",
                        blocked_by=blocked_by_ids,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._emit_event(
                        run.id,
                        self._make_event(
                            "task_blocked",
                            agent_id=task.agent_id,
                            task_id=tid,
                            data={"blocked_by": blocked_by_ids, "reason": reason},
                        ),
                    )
                    continue

                agent_spec = agent_map.get(task.agent_id)
                if agent_spec is None:
                    results[tid] = WorkerResult(
                        status="failed",
                        summary="",
                        error=f"Agent '{task.agent_id}' not found in presets",
                    )
                    continue

                # Mark task as in_progress
                task_store.update_status(
                    tid,
                    TaskStatus.in_progress,
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                self._emit_event(
                    run.id,
                    self._make_event("task_started", agent_id=agent_spec.id, task_id=tid),
                )

                # 按 task.input_from（"上下文变量名" -> "来源任务 id"）把上游
                # 已完成任务的摘要组装成这个任务能看到的上游上下文，会拼进
                # worker 的 prompt 里（见 worker.py）。
                upstream: dict[str, str] = {}
                for context_key, source_task_id in task.input_from.items():
                    if source_task_id in task_summaries:
                        upstream[context_key] = task_summaries[source_task_id]

                # 提交到线程池异步执行，不阻塞当前循环去派发同层的下一个任务；
                # 真正的执行体是 _run_worker_with_retries（内含失败重试）。
                future = executor.submit(
                    self._run_worker_with_retries,
                    agent_spec=agent_spec,
                    task=task,
                    upstream_summaries=upstream,
                    user_vars=run.user_vars,
                    run_dir=run_dir,
                    event_callback=_event_callback,
                    run_id=run.id,
                    include_shell_tools=include_shell_tools,
                    grounding_block=grounding_block,
                )
                futures[future] = tid
                # 单个任务最坏情况耗时 = 单次超时 x (重试次数 + 1)；整层的
                # deadline 取本层所有任务里最大的那个（因为要等最慢的跑完）。
                per_task_budget = agent_spec.timeout_seconds * (agent_spec.max_retries + 1)
                layer_budget = max(layer_budget, per_task_budget)

            # Collect results with a hard layer-level deadline — defends against
            # worker threads stuck in C extensions / blocked I/O that bypass the
            # in-loop timeout check (issue #42).
            deadline_buffer = 60
            layer_deadline = layer_budget + deadline_buffer if layer_budget else None

            try:
                # as_completed 按完成顺序（不是提交顺序）逐个拿结果；单个 future
                # 内部抛异常也要兜住，避免一个 worker 挂了拖垮整层收集逻辑。
                for future in as_completed(futures, timeout=layer_deadline):
                    tid = futures[future]
                    try:
                        results[tid] = future.result()
                    except Exception as exc:
                        logger.error("Worker for task %s raised exception", tid, exc_info=True)
                        results[tid] = WorkerResult(
                            status="failed",
                            summary="",
                            error=str(exc),
                        )
            except FuturesTimeoutError:
                # 整层硬超时兜底：即使某个 worker 卡在阻塞 I/O / C 扩展里，
                # 导致内部的逐任务超时检查失效，这里也能强制把它标记为
                # timeout 并继续往下走，不会让整个 run 卡死。
                for pending, tid in futures.items():
                    if tid in results:
                        continue
                    pending.cancel()
                    logger.error(
                        "Worker for task %s exceeded layer deadline (%ds)",
                        tid,
                        layer_deadline,
                    )
                    results[tid] = WorkerResult(
                        status="timeout",
                        summary="",
                        error=f"Worker exceeded layer deadline of {layer_deadline}s",
                    )
        except KeyboardInterrupt:
            cancel_event.set()
            logger.warning("Swarm layer interrupted — cancelling pending workers")
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    def _run_worker_with_retries(
        self,
        agent_spec: SwarmAgentSpec,
        task: SwarmTask,
        upstream_summaries: dict[str, str],
        user_vars: dict[str, str],
        run_dir: Path,
        event_callback: Callable[[SwarmEvent], None] | None,
        run_id: str,
        include_shell_tools: bool = False,
        grounding_block: str = "",
    ) -> WorkerResult:
        """Run a worker with automatic retry on failure.

        Retries up to agent_spec.max_retries times. Emits a "task_retry" event
        before each retry attempt. Token counts are accumulated across all
        attempts.

        Args:
            agent_spec: Agent role specification.
            task: The task to execute.
            upstream_summaries: Summaries from upstream tasks.
            user_vars: User-provided template variables.
            run_dir: Run directory path.
            event_callback: Optional event callback.
            run_id: Run identifier for event emission.
            include_shell_tools: Whether the worker may register shell tools.
            grounding_block: Pre-rendered "Ground Truth" markdown spliced
                into the worker's system prompt. Empty string when no
                symbols were extracted from user_vars.

        Returns:
            WorkerResult from the last attempt.
        """
        max_retries = agent_spec.max_retries
        cumulative_input_tokens = 0
        cumulative_output_tokens = 0
        result: WorkerResult | None = None

        # 最多跑 max_retries + 1 次（首次尝试 + N 次重试）。注意只有
        # status == "failed"（worker 内部异常/工具报错等）才会触发重试；
        # "timeout"、"token_limit" 等状态被视为确定性失败，重试也大概率
        # 是同样结果，所以下面 `result.status != "failed"` 直接判定为终态。
        for attempt in range(max_retries + 1):
            if attempt > 0:
                self._emit_event(
                    run_id,
                    self._make_event(
                        "task_retry",
                        agent_id=agent_spec.id,
                        task_id=task.id,
                        data={
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "previous_error": result.error if result else None,
                        },
                    ),
                )
                logger.info(
                    "Retrying task %s (attempt %d/%d)",
                    task.id,
                    attempt + 1,
                    max_retries + 1,
                )

            result = run_worker(
                agent_spec=agent_spec,
                task=task,
                upstream_summaries=upstream_summaries,
                user_vars=user_vars,
                run_dir=run_dir,
                event_callback=event_callback,
                include_shell_tools=include_shell_tools,
                grounding_block=grounding_block,
                agent_config=self._agent_config,
            )

            cumulative_input_tokens += result.input_tokens
            cumulative_output_tokens += result.output_tokens

            if result.status != "failed":
                # Success (or timeout/token_limit/completed) — no more retries
                result = result.model_copy(
                    update={
                        "input_tokens": cumulative_input_tokens,
                        "output_tokens": cumulative_output_tokens,
                    }
                )
                return result

        # All retries exhausted, return the last failed result with cumulative tokens
        if result is not None:
            result = result.model_copy(
                update={
                    "input_tokens": cumulative_input_tokens,
                    "output_tokens": cumulative_output_tokens,
                }
            )
        return result  # type: ignore[return-value]

    def _cancel_remaining_tasks(
        self,
        task_store: TaskStore,
        current_layer_ids: list[str],
        all_tasks: list[SwarmTask],
    ) -> None:
        """Mark all non-completed tasks as cancelled.

        Args:
            task_store: TaskStore for persistence.
            current_layer_ids: Task IDs in the current (interrupted) layer.
            all_tasks: All tasks in the run.
        """
        for task in all_tasks:
            if task.status not in (TaskStatus.completed, TaskStatus.failed):
                try:
                    task_store.update_status(task.id, TaskStatus.cancelled)
                except Exception:
                    logger.warning("Failed to cancel task %s", task.id, exc_info=True)
