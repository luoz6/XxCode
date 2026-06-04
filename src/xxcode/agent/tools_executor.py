"""StreamingToolExecutor — concurrent tool execution during API streaming.

"解析即触发，只读可并发":
- Read-only + concurrency_safe tools start immediately as background tasks
- Write/non-safe tools are queued until permission is resolved post-stream
- Non-safe failure triggers cascading cancel of all in-flight safe tasks
- Bash tool failure triggers sibling abort of all other bash tools
- Progress events streamed via AsyncQueue in context for live UI updates
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from ..tools import ToolCall
from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Cap concurrent safe-tool executions to avoid I/O storms when the model
# fires off dozens of read_file / grep_search calls at once.
DEFAULT_MAX_CONCURRENT_SAFE_TOOLS = 10


class ToolState(Enum):
    QUEUED = auto()      # Waiting for streaming to end or resources freed
    EXECUTING = auto()   # Running
    COMPLETED = auto()   # Finished successfully or with error
    ABORTED = auto()     # Cancelled by sibling abort controller
    YIELDED = auto()     # Result harvested by caller


@dataclass
class _Slot:
    """Per-tool execution slot tracking state, task, and result."""

    tc: ToolCall
    state: ToolState = ToolState.QUEUED
    task: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None
    is_error: bool = False
    truncated: str = ""
    permission_granted: bool = False
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)


class _NotifyingQueue(asyncio.Queue[dict[str, Any]]):
    """Queue that notifies the executor when new progress arrives."""

    def __init__(self, on_activity: Callable[[], None]):
        super().__init__()
        self._on_activity = on_activity

    async def put(self, item: dict[str, Any]) -> None:
        await super().put(item)
        self._on_activity()

    def put_nowait(self, item: dict[str, Any]) -> None:
        super().put_nowait(item)
        self._on_activity()


class StreamingToolExecutor:
    """Execute tools concurrently with "safe tools in parallel, non-safe serialized" policy.

    Features:
      - Dynamic concurrency scheduling with mutex rules
      - Sibling abort controller for bash tool failure cascading
      - Progress event streaming via AsyncQueue in execution context
      - FIFO result ordering regardless of completion order

    Lifecycle:
      1. During API streaming: add_tool() as each tool_use block is parsed.
         Safe tools start immediately; unsafe tools queue up.
      2. After streaming: resolve permissions for queued tools, then start them.
      3. Harvest: get_remaining_results() blocks until all complete, returns
         results in original tool-call order.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        config: Any,
        state: Any,
        context: dict[str, Any],
        max_concurrent_safe: int = DEFAULT_MAX_CONCURRENT_SAFE_TOOLS,
    ):
        self._registry = registry
        self._config = config
        self._state = state
        self._context = context

        self._slots: dict[str, _Slot] = {}
        self._tool_order: list[str] = []

        # Concurrency control
        self._cancel_event = asyncio.Event()
        self._non_safe_failed = False
        self._non_safe_running = 0
        self._safe_semaphore = asyncio.Semaphore(max_concurrent_safe)

        # Progress streaming — injected into context so tools can push
        # incremental output chunks during execution.
        self._activity_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._progress_queue: asyncio.Queue[dict[str, Any]] = _NotifyingQueue(self._signal_activity)
        self._context["progress_queue"] = self._progress_queue

    # ── Public API ───────────────────────────────────────────────────

    def add_tool(self, tc: ToolCall) -> bool:
        """Called during streaming when a tool_use block is parsed.

        If the tool is concurrency-safe and no non-safe tool is running,
        it starts immediately in the background. Otherwise it's queued.

        Returns:
            True if the tool was started immediately, False if queued.
        """
        slot = _Slot(tc=tc)
        self._slots[tc.id] = slot
        self._tool_order.append(tc.id)

        if self._can_start_immediately(tc):
            self._launch(tc.id)
            return True
        return False

    def try_start_queued(self, tc_id: str) -> bool:
        """Attempt to start a queued tool. Fails if a non-safe tool is running."""
        slot = self._slots.get(tc_id)
        if slot is None or slot.state != ToolState.QUEUED:
            return False
        slot.permission_granted = True
        if not self._can_start_immediately(slot.tc, allow_non_safe=True):
            return False
        self._launch(tc_id)
        return True

    def deny_tool(self, tc_id: str) -> None:
        """Mark a queued tool as denied by the user."""
        slot = self._slots[tc_id]
        slot.state = ToolState.COMPLETED
        slot.result = {
            "type": "tool_result",
            "tool_use_id": tc_id,
            "content": "User denied this action.",
        }
        slot.is_error = False

    def is_queued(self, tc_id: str) -> bool:
        """Check if a tool is still queued (awaiting permission)."""
        slot = self._slots.get(tc_id)
        return slot is not None and slot.state == ToolState.QUEUED

    def is_running(self, tc_id: str) -> bool:
        """Check if a tool is currently executing."""
        slot = self._slots.get(tc_id)
        return slot is not None and slot.state == ToolState.EXECUTING

    def get_completed_results(self) -> list[dict[str, Any]]:
        """Non-blocking: harvest completed but not-yet-yielded results.

        Used during streaming to display early tool results.
        Each result is yielded at most once — after this call its state
        transitions to YIELDED.  ABORTED slots are also harvested here.
        """
        results: list[dict[str, Any]] = []
        for tid in self._tool_order:
            slot = self._slots.get(tid)
            if slot is not None and slot.state in (ToolState.COMPLETED, ToolState.ABORTED):
                slot.state = ToolState.YIELDED
                if slot.result is not None:
                    results.append(slot.result)
        return results

    def has_pending_work(self) -> bool:
        """Return True while any tool is queued or executing."""
        return any(
            slot.state in (ToolState.QUEUED, ToolState.EXECUTING)
            for slot in self._slots.values()
        )

    async def wait_for_activity(self) -> None:
        """Block until a tool completes or emits progress."""
        if not self.has_pending_work():
            return
        if not any(slot.state == ToolState.EXECUTING for slot in self._slots.values()):
            self._maybe_drain()
            if not any(slot.state == ToolState.EXECUTING for slot in self._slots.values()):
                return
        await self._activity_queue.get()

    async def get_remaining_results(self) -> list[dict[str, Any]]:
        """Block until all tools complete, return results in original order.

        Uses a while-loop because _maybe_drain dynamically spawns new tasks
        when a non-safe tool finishes. A single gather would miss those.

        Result order is FIFO by _tool_order — even if later tools finish
        first, results are always yielded in the original call order.

        Tier 3 aggregate budget (200K) is enforced on the final result set
        to prevent concurrent tool outputs from overflowing the context window.
        """
        while True:
            tasks = [
                s.task
                for s in self._slots.values()
                if s.task is not None and not s.task.done()
            ]

            if not tasks:
                # No running tasks — check for stalled queued slots.
                queued = [s for s in self._slots.values() if s.state == ToolState.QUEUED]
                if queued and not self._cancel_event.is_set():
                    logger.warning("Found stalled queued tasks, force draining...")
                    self._maybe_drain()
                    continue
                break

            await asyncio.gather(*tasks, return_exceptions=True)

        # Build result list in original tool order.
        results: list[dict[str, Any]] = []
        for tid in self._tool_order:
            slot = self._slots.get(tid)
            if slot is None:
                continue
            if slot.state == ToolState.YIELDED:
                continue
            if slot.result is not None:
                results.append(slot.result)

        # Tier 3: Aggregate per-message budget (200K default).
        total = sum(len(r.get("content", "")) for r in results)
        if total > self._config.max_message_tool_results_chars and len(results) > 1:
            from xxcode.core.budget import apply_aggregate_result_budget
            results = await apply_aggregate_result_budget(
                results, self._config.max_message_tool_results_chars,
            )

        return results

    def drain_progress(self) -> list[dict[str, Any]]:
        """Non-blocking: drain all pending progress events from the queue.

        Returns a list of progress dicts, each containing:
          {"tool_use_id": str, "tool_name": str, "chunk": str}

        The caller (agent loop) yields these as StreamEvent(type="tool_progress").
        """
        events: list[dict[str, Any]] = []
        while not self._progress_queue.empty():
            try:
                events.append(self._progress_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    def _signal_activity(self) -> None:
        try:
            self._activity_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def abort_siblings(self, failed_tool_id: str) -> None:
        """Cancel all other bash tools when one bash tool fails.

        Called from the agent loop's post-hook section when a run_shell
        tool returns an error.  All queued or executing bash tools are
        cancelled to prevent cascading failures from interdependent
        shell commands.
        """
        failed_slot = self._slots.get(failed_tool_id)
        if failed_slot is None:
            return

        aborted_count = 0
        for tid, slot in self._slots.items():
            if tid == failed_tool_id:
                continue
            if slot.state not in (ToolState.QUEUED, ToolState.EXECUTING):
                continue

            # Only cancel tools that participate in sibling abort (bash tools).
            tool = self._registry.get(slot.tc.name)
            if tool is None or not tool.supports_sibling_abort():
                continue

            # Signal the tool to stop cooperatively, then cancel its task.
            slot.abort_event.set()
            if slot.task is not None and not slot.task.done():
                slot.task.cancel()

            slot.state = ToolState.ABORTED
            slot.is_error = True
            slot.result = {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": (
                    f"Execution aborted — sibling bash tool "
                    f"'{failed_slot.tc.name}' ({failed_tool_id}) failed."
                ),
            }
            aborted_count += 1
            logger.debug(
                "Sibling abort: cancelled %s due to %s failure",
                tid, failed_tool_id,
            )

        if aborted_count:
            # Cascading cancel safe tools too — the environment may be broken.
            self._cancel_event.set()

        logger.info(
            "Sibling abort controller: aborted %d bash tools due to %s failure",
            aborted_count, failed_tool_id,
        )

    def get_slot(self, tc_id: str) -> _Slot | None:
        """Return the internal slot for a tool (used by agent for StreamEvent metadata)."""
        return self._slots.get(tc_id)

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ── Internal ─────────────────────────────────────────────────────

    def _maybe_drain(self) -> None:
        """After a tool completes, try to start any queued tools.

        Priority ordering:
          1. Safe tools first — maximise streaming overlap coverage.
          2. Non-safe tools second — serialised, only one at a time.

        Called from _execute_one's finally block.
        """
        if self._cancel_event.is_set():
            return

        # Priority 1: launch safe tools to maximise concurrent coverage.
        for tid in self._tool_order:
            slot = self._slots.get(tid)
            if slot is not None and slot.state == ToolState.QUEUED:
                if self._is_safe(slot.tc) and self._can_start_immediately(slot.tc):
                    self._launch(tid)

        # Priority 2: launch one non-safe tool (serialised).
        for tid in self._tool_order:
            slot = self._slots.get(tid)
            if slot is not None and slot.state == ToolState.QUEUED:
                if (
                    not self._is_safe(slot.tc)
                    and slot.permission_granted
                    and self._can_start_immediately(slot.tc, allow_non_safe=True)
                ):
                    self._launch(tid)
                    return  # Only one non-safe at a time

    def _can_start_immediately(
        self,
        tc: ToolCall,
        *,
        allow_non_safe: bool = False,
    ) -> bool:
        """Check whether a tool can start right now.

        Rules:
          1. Cancel event set → block everything.
          2. Non-safe tool running → block everything (mutex).
          3. Non-safe tool → always queue (needs permission first).
          4. Otherwise → allow (safe tool, no mutex held).
        """
        if self._cancel_event.is_set():
            return False
        if self._non_safe_running > 0:
            return False
        if not self._is_safe(tc) and not allow_non_safe:
            return False
        return True

    def _is_safe(self, tc: ToolCall) -> bool:
        """Determine if a tool is concurrency-safe (input-aware)."""
        tool = self._registry.get(tc.name)
        if tool is None:
            return False
        try:
            validated = tool.input_schema.model_validate(tc.input)
        except Exception:
            validated = None
        return tool.is_concurrency_safe(validated)

    def _launch(self, tc_id: str) -> None:
        """Create a background task for a tool and track it."""
        slot = self._slots[tc_id]
        slot.state = ToolState.EXECUTING

        is_safe = self._is_safe(slot.tc)
        if not is_safe:
            self._non_safe_running += 1

        slot.task = asyncio.create_task(self._execute_one(tc_id, is_safe))

    async def _execute_one(self, tc_id: str, is_safe: bool) -> None:
        """Execute a single tool and store the result in its slot.

        This is the reusable execution core — called as a background task.
        Handles cache replay, budget, abort checks, and cascading cancel.

        The tool's context dict contains:
          - "progress_queue": asyncio.Queue for streaming progress chunks
          - "abort_event":    asyncio.Event set by sibling abort controller
        """
        slot = self._slots[tc_id]
        tc = slot.tc

        # Inject abort_event into context so the tool can check it
        # cooperatively during long-running operations.
        exec_context = {**self._context, "abort_event": slot.abort_event}

        try:
            # Check cancel / abort signals before starting work.
            if self._cancel_event.is_set() or slot.abort_event.is_set():
                slot.result = {
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": "Execution cancelled — a prior write tool failed.",
                }
                slot.is_error = True
                return

            # Replay from content_replacements cache (resume / prefix-cache).
            if tc_id in self._state.content_replacements:
                slot.truncated = self._state.content_replacements[tc_id]
                slot.result = {
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": slot.truncated,
                }
                slot.is_error = False
                return

            # Execute via registry (safe tools are throttled by semaphore).
            if is_safe:
                async with self._safe_semaphore:
                    result = await self._registry.execute(tc, exec_context)
            else:
                result = await self._registry.execute(tc, exec_context)
            slot.is_error = result.is_error

            # Apply tool-result budget via the tool's own formatter.
            # Uses per-tool max_output_chars (Tier 1), with Tier 2 absolute
            # ceiling enforced inside format_large_result itself.
            tool = self._registry.get(tc.name)
            if tool is not None and not result.is_error:
                max_chars = tool.get_max_output_chars()
                slot.truncated = await tool.format_large_result(
                    content=result.content,
                    max_chars=max_chars,
                    tool_use_id=tc_id,
                    session_dir=str(self._config.session_dir),
                )
            else:
                slot.truncated = result.content

            # Persist for resume replay.
            self._state.content_replacements[tc_id] = slot.truncated

            slot.result = {
                "type": "tool_result",
                "tool_use_id": tc_id,
                "content": slot.truncated,
            }

            # Cascading cancel: non-safe failure → kill all in-flight tasks.
            if result.is_error and not is_safe:
                self._non_safe_failed = True
                self._cancel_event.set()
                for other_id, other_slot in self._slots.items():
                    if (
                        other_id != tc_id
                        and other_slot.task is not None
                        and not other_slot.task.done()
                    ):
                        other_slot.abort_event.set()
                        other_slot.task.cancel()
                        logger.debug(
                            "Cascading cancel: aborting %s due to %s failure",
                            other_id, tc.name,
                        )

        except asyncio.CancelledError:
            if slot.abort_event.is_set():
                # Aborted by sibling abort controller — message already set
                # by abort_siblings().  Only fill in if not already set.
                if slot.result is None:
                    slot.result = {
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": "Execution aborted — sibling tool failed.",
                    }
                    slot.is_error = True
            else:
                # Cancelled by cascading cancel.
                slot.result = {
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": "Execution cancelled — a prior write tool failed.",
                }
                slot.is_error = True

        except Exception as exc:
            logger.exception("Tool %s execution failed", tc.name)
            slot.result = {
                "type": "tool_result",
                "tool_use_id": tc_id,
                "content": f"Error executing '{tc.name}': {exc}",
            }
            slot.is_error = True

        finally:
            if not is_safe:
                self._non_safe_running -= 1
            # Preserve ABORTED state if set by abort_siblings().
            if slot.state != ToolState.ABORTED:
                slot.state = ToolState.COMPLETED
            self._maybe_drain()
            self._signal_activity()
