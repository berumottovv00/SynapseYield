"""Shared serialization helpers for the swarm read boundaries.

Single source of truth for projecting a :class:`SwarmTask` into the per-task
JSON dict returned by the MCP tools (``run_swarm`` / ``get_swarm_status`` /
``get_run_result``) and the in-process ``run_swarm`` agent tool.

Before this module each boundary hand-maintained its own field allowlist and
all three silently omitted ``SwarmTask.error``: a misconfigured provider
produced ``status="failed"`` with no diagnosable reason anywhere the caller
could see, even though the error was captured on disk (see P04).
"""

# 序列化指把内存中的对象（比如一个 Python 类实例，里面有各种属性、嵌套对象、枚举等复杂结构）转换成一种简单、可传输/可存储的格式（比如
# JSON、字符串、字节流）。它的反向操作叫反序列化（deserialization）——把这种简单格式还原回对象。

from __future__ import annotations

from typing import Any

from src.tools.redaction import redact_internal_paths


def serialize_task(task: Any) -> dict:
    """Project a SwarmTask into its public per-task dict.

    ``error`` and ``iterations`` are always included so a failed or degraded
    task is diagnosable from every read path, not only the on-disk artifacts.
    """
    status = task.status.value if hasattr(task.status, "value") else str(task.status)
    return {
        "id": task.id,
        "agent_id": task.agent_id,
        "status": status,
        "summary": task.summary,
        "iterations": getattr(task, "worker_iterations", 0),
        "error": redact_internal_paths(getattr(task, "error", None)) or None,
        "started_at": getattr(task, "started_at", None),
        "completed_at": getattr(task, "completed_at", None),
        "depends_on": list(getattr(task, "depends_on", []) or []),
        "blocked_by": list(getattr(task, "blocked_by", []) or []),
    }


# - 输入：一个 run 对象（一次 swarm 运行，里面包含多个 tasks）。
# - 逻辑：遍历这个 run 下的所有任务，找到第一个有 error 的任务，把它的错误信息格式化成 "任务ID/agent_id: 错误内容" 这样一行字符串返回。
# - 如果所有任务都没出错，显式返回 None（而不是不返回这个字段），这样调用方哪怕只看 run 的顶层结果，也能知道"这次运行是否有错"，不需要再去逐个任务里翻找。
# - error 字段用 redact_internal_paths(...) 做了脱敏处理（比如把服务器上的绝对路径去掉），避免把内部文件系统结构泄露给调用方；同时用 or None 确保空字符串也统一变成 None。
def run_level_error(run: Any) -> str | None:
    """First failed task's error, for a top-level ``error`` field.

    Returns ``None`` (an explicit null, not an absent key) when no task carries
    an error, so a caller that only reads the top level still gets a signal.
    """
    for task in getattr(run, "tasks", None) or []:
        err = getattr(task, "error", None)
        if err:
            return f"{task.id}/{task.agent_id}: {redact_internal_paths(err)}"
    return None
