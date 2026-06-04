"""AgentState — concentrated session state for the coding agent."""

from dataclasses import dataclass, field
from typing import Any

from ..security.permission import PermissionState
from ..tools.file_edit.types import FileStateEntry


@dataclass
class AgentState:
    """Concentrated agent state for a coding session.

    Mutated in place by the agent loop during each turn.  This is safe
    because the CoreExecutionEngine runs single-threaded (asyncio
    cooperative multitasking) with a single _query_loop per instance
    — there is no concurrent access to the same AgentState.

    If concurrent query engines are introduced in the future, protect
    state access with asyncio.Lock rather than attempting immutable
    copy-on-write (which is prohibitively expensive for large message
    histories).
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    server_tool_use_input_tokens: int = 0
    turn_count: int = 0
    consecutive_autocompact_failures: int = 0
    last_continue_reason: str = ""

    # Permission tracking
    permission_state: PermissionState = field(default_factory=PermissionState)

    # Denial tracking — maps tool_name → consecutive user denial count.
    # Reset when the user grants or the tool succeeds.  If a tool hits 3,
    # the loop injects a <system_hint> to prevent the model from looping
    # on the same denied action forever.
    denied_tool_calls: dict[str, int] = field(default_factory=dict)

    # Tool error tracking — maps tool_name → consecutive execution error count.
    # SEPARATE from denied_tool_calls because "user said no" and "tool crashed"
    # have different root causes and different anti-deadloop messaging.
    # Reset when the tool succeeds.  Trigger anti-deadloop hint at 3+.
    tool_errors: dict[str, int] = field(default_factory=dict)

    # Session
    system_prompt: str = ""

    # Error tracking for normalization Step 3 (error→block-type mapping)
    recent_api_errors: list[str] = field(default_factory=list)

    # Exact truncated tool outputs keyed by tool_use_id — guarantees
    # resumed sessions replay identical content for prefix-cache hits.
    content_replacements: dict[str, str] = field(default_factory=dict)

    # Task budget tracking (carried across autocompact boundaries).
    total_task_budget: int | None = None
    task_budget_remaining: int | None = None

    # Rolling history cache: absolute indices of messages marked with
    # cache_control.  Positions are sticky — once placed they stay fixed
    # to avoid invalidating Anthropic prompt cache segments.
    cache_breakpoints: set[int] = field(default_factory=set)

    # Read-file state cache — maps absolute file path → FileStateEntry.
    # Populated by read_file tool, consumed by edit_file / write_file tools
    # for read-before-edit enforcement and external-modification detection.
    read_file_state: dict[str, object] = field(default_factory=dict)

    # Memory recall tracking — filenames of memories already surfaced this session.
    # Pre-filtered before each recall query to avoid wasting the 5-slot budget.

    # Memory extraction tracking — for throttling and mutual exclusion
    last_extraction_turn: int = 0
    user_turn_count: int = 0
    last_extraction_user_turn: int = 0
    memory_writes_since_extraction: bool = False

    # Last user query text — saved for memory recall context
    last_query: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict for session persistence."""
        return {
            "messages": self.messages,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "server_tool_use_input_tokens": self.server_tool_use_input_tokens,
            "turn_count": self.turn_count,
            "consecutive_autocompact_failures": self.consecutive_autocompact_failures,
            "last_continue_reason": self.last_continue_reason,
            "permission_state": self.permission_state.to_dict(),
            "system_prompt": self.system_prompt,
            "recent_api_errors": self.recent_api_errors,
            "content_replacements": self.content_replacements,
            "denied_tool_calls": self.denied_tool_calls,
            "tool_errors": self.tool_errors,
            "total_task_budget": self.total_task_budget,
            "task_budget_remaining": self.task_budget_remaining,
            "cache_breakpoints": list(self.cache_breakpoints),
            "read_file_state": {
                k: v.to_dict() if hasattr(v, "to_dict") else v
                for k, v in self.read_file_state.items()
            },
            "last_query": self.last_query,
            "last_extraction_turn": self.last_extraction_turn,
            "user_turn_count": self.user_turn_count,
            "last_extraction_user_turn": self.last_extraction_user_turn,
            "memory_writes_since_extraction": self.memory_writes_since_extraction,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AgentState":
        """Deserialize from a dict produced by to_dict()."""
        return cls(
            messages=data.get("messages", []),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            cache_read_input_tokens=data.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=data.get("cache_creation_input_tokens", 0),
            server_tool_use_input_tokens=data.get("server_tool_use_input_tokens", 0),
            turn_count=data.get("turn_count", 0),
            consecutive_autocompact_failures=data.get("consecutive_autocompact_failures", 0),
            last_continue_reason=data.get("last_continue_reason", ""),
            permission_state=PermissionState.from_dict(data.get("permission_state", {})),
            system_prompt=data.get("system_prompt", ""),
            recent_api_errors=data.get("recent_api_errors", []),
            content_replacements=data.get("content_replacements", {}),
            denied_tool_calls=data.get("denied_tool_calls", {}),
            tool_errors=data.get("tool_errors", {}),
            total_task_budget=data.get("total_task_budget"),
            task_budget_remaining=data.get("task_budget_remaining"),
            cache_breakpoints=set(data.get("cache_breakpoints", [])),
            read_file_state=_deserialize_read_file_state(
                data.get("read_file_state", {})
            ),
            last_query=data.get("last_query", ""),
            last_extraction_turn=data.get("last_extraction_turn", 0),
            user_turn_count=data.get("user_turn_count", 0),
            last_extraction_user_turn=data.get("last_extraction_user_turn", 0),
            memory_writes_since_extraction=data.get("memory_writes_since_extraction", False),
        )


def _deserialize_read_file_state(raw: dict) -> dict[str, FileStateEntry]:
    """Convert serialized dicts back to FileStateEntry objects."""
    result: dict[str, FileStateEntry] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            result[k] = FileStateEntry.from_dict(v)
        elif isinstance(v, FileStateEntry):
            result[k] = v
    return result
