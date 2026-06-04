"""Background task management for long-running shell commands.

Two modes:
  1. Explicit background: model sets run_in_background=True
  2. Auto-background: blocking commands > 15s automatically move to background

Background tasks are tracked as asyncio Tasks, with output written to
disk files that the model can poll via read_file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_IS_WINDOWS = platform.system() == "Windows"

# ── Constants ─────────────────────────────────────────────────────────

# After this many seconds, a blocking foreground command is
# automatically moved to the background.
ASSISTANT_BLOCKING_BUDGET_MS = 15_000  # 15 seconds

# Maximum number of concurrent background tasks.
MAX_BACKGROUND_TASKS = 10


@dataclass
class BackgroundTask:
    """A shell command running in the background."""
    task_id: str
    command: str
    description: str = ""
    output_path: str = ""
    error_path: str = ""
    status: str = "running"  # running, completed, failed, cancelled
    exit_code: int | None = None
    start_time: float = 0.0
    end_time: float | None = None


class BackgroundManager:
    """Manages background shell tasks.

    Tracks tasks in a dict, writes output to session_dir/tool-results/,
    and enforces concurrency limits.
    """

    def __init__(self, session_dir: str | Path):
        self._tasks: dict[str, BackgroundTask] = {}
        self._asyncio_tasks: dict[str, asyncio.Task] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._session_dir = Path(session_dir)
        self._results_dir = self._session_dir / "tool-results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == "running")

    def can_accept(self) -> bool:
        return self.active_count < MAX_BACKGROUND_TASKS

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_active(self) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def list_all(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    async def start_background(
        self,
        command: str,
        description: str = "",
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> BackgroundTask:
        """Start a command in the background.

        Args:
            command: Shell command to execute.
            description: Human-readable description.
            cwd: Working directory.
            timeout: Optional timeout. If None, no timeout.

        Returns:
            BackgroundTask with task_id and output/error paths.
        """
        async with self._lock:
            if not self.can_accept():
                raise RuntimeError(
                    f"Too many background tasks ({self.active_count}). "
                    f"Wait for some to complete."
                )

            task_id = f"bg-{uuid.uuid4().hex[:8]}"
            output_path = self._results_dir / f"{task_id}.txt"
            error_path = self._results_dir / f"{task_id}.error.txt"

            task = BackgroundTask(
                task_id=task_id,
                command=command,
                description=description,
                output_path=str(output_path),
                error_path=str(error_path),
                start_time=asyncio.get_event_loop().time(),
            )

            self._tasks[task_id] = task

        # Start the actual async task.
        async_task = asyncio.create_task(
            self._run_task(task, cwd or ".", timeout),
            name=f"bg-shell-{task_id}",
        )
        self._asyncio_tasks[task_id] = async_task

        # Clean up when done.
        async_task.add_done_callback(
            lambda t: self._on_task_done(task_id, t)
        )

        return task

    async def _run_task(
        self, task: BackgroundTask, cwd: str, timeout: float | None,
    ) -> None:
        """Execute the command and write output to disk."""
        try:
            proc = await asyncio.create_subprocess_shell(
                task.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                **_process_group_kwargs(),
            )
            self._processes[task.task_id] = proc

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                await _terminate_process_tree(proc)
                await proc.wait()
                output = f"[Command timed out after {timeout}s]\n"
                output += f"Command: {task.command}\n"
                Path(task.output_path).write_text(output, encoding="utf-8")
                Path(task.error_path).write_text(
                    f"Timeout after {timeout}s", encoding="utf-8",
                )
                task.status = "failed"
                return

            exit_code = proc.returncode or 0
            task.exit_code = exit_code

            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                output += "\n[stderr]\n" + stderr.decode("utf-8", errors="replace")

            from .command_semantics import format_exit_code
            exit_info = format_exit_code(task.command, exit_code)
            output = f"{exit_info}\n\n{output}"

            Path(task.output_path).write_text(output, encoding="utf-8")
            task.status = "completed"

        except Exception as exc:
            Path(task.error_path).write_text(
                f"Background task crashed: {exc}", encoding="utf-8",
            )
            task.status = "failed"
            logger.exception("Background task %s failed", task.task_id)

    def _on_task_done(self, task_id: str, _asyncio_task: asyncio.Task) -> None:
        """Callback when async task completes."""
        task = self._tasks.get(task_id)
        if task:
            task.end_time = asyncio.get_event_loop().time()
        self._asyncio_tasks.pop(task_id, None)
        logger.info(
            "Background task %s completed: status=%s",
            task_id, task.status if task else "unknown",
        )

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running background task, killing the subprocess."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status != "running":
            return False

        # Kill the underlying OS process first.
        proc = self._processes.pop(task_id, None)
        if proc is not None and proc.returncode is None:
            try:
                await _terminate_process_tree(proc)
                await proc.wait()
            except Exception:
                pass

        # Then cancel the asyncio task wrapper.
        async_task = self._asyncio_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            try:
                await async_task
            except asyncio.CancelledError:
                pass

        task.status = "cancelled"
        task.end_time = asyncio.get_event_loop().time()
        return True

    async def wait_for(self, task_id: str, timeout: float | None = None) -> BackgroundTask | None:
        """Wait for a background task to complete."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.status != "running":
            return task

        async_task = self._asyncio_tasks.get(task_id)
        if async_task is None:
            return task

        try:
            await asyncio.wait_for(
                asyncio.shield(async_task), timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None

        return self._tasks.get(task_id)

    def get_output(self, task_id: str) -> str | None:
        """Read the output file for a completed/failed task."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        try:
            return Path(task.output_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def get_error(self, task_id: str) -> str | None:
        """Read the error file for a failed task."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        try:
            return Path(task.error_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception:
            return None


# ── Auto-background helper ────────────────────────────────────────────

async def auto_background_if_blocking(
    start_time: float,
    manager: BackgroundManager,
    command: str,
    description: str = "",
    cwd: str | None = None,
) -> BackgroundTask | None:
    """Move a blocking foreground command to background if it exceeds budget.

    Call this after ASSISTANT_BLOCKING_BUDGET_MS has elapsed while
    waiting for a command to complete.

    Returns the BackgroundTask if moved, None if budget not exceeded.
    """
    elapsed = (asyncio.get_event_loop().time() - start_time) * 1000
    if elapsed >= ASSISTANT_BLOCKING_BUDGET_MS and manager.can_accept():
        task = await manager.start_background(
            command=command,
            description=description,
            cwd=cwd,
        )
        logger.info(
            "Auto-background: '%s' moved to background (elapsed: %.0fms)",
            description or command[:60], elapsed,
        )
        return task
    return None


def _process_group_kwargs() -> dict[str, Any]:
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    try:
        if _IS_WINDOWS:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except FileNotFoundError:
        proc.kill()
    except Exception:
        proc.kill()

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
