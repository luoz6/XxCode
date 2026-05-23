"""Permission checking pipeline."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PermissionState:
    """Tracks which paths and tools have been approved in this session."""

    confirmed_paths: set[str] = field(default_factory=set)
    confirmed_tools: set[str] = field(default_factory=set)
    yolo_mode: bool = False

    def is_path_confirmed(self, path: str) -> bool:
        """Check if a path (or any ancestor) has been confirmed."""
        if self.yolo_mode:
            return True
        p = Path(path).resolve()
        parts = str(p).replace("\\", "/").split("/")
        # Check prefixes
        for i in range(len(parts) + 1):
            prefix = "/".join(parts[:i]) or "/"
            if prefix in self.confirmed_paths:
                return True
        return False

    def confirm_path(self, path: str) -> None:
        """Mark a path as confirmed."""
        p = Path(path).resolve()
        self.confirmed_paths.add(str(p))

    def is_tool_confirmed(self, tool_name: str) -> bool:
        """Check if a tool invocation has been pre-approved."""
        if self.yolo_mode:
            return True
        return tool_name in self.confirmed_tools

    def confirm_tool(self, tool_name: str) -> None:
        """Mark a tool as pre-approved for this session."""
        self.confirmed_tools.add(tool_name)


def needs_user_permission(tool_name: str, tool_input: Any, state: PermissionState) -> bool:
    """Determine if this tool invocation requires user confirmation.

    Rules (in order):
    1. YOLO mode — skip all confirmations
    2. Tool already confirmed this session — skip
    3. Read-only tool — skip (auto-allow)
    4. Path already confirmed — skip
    5. Otherwise — requires confirmation
    """
    if state.yolo_mode:
        return False

    if state.is_tool_confirmed(tool_name):
        return False

    # Check if there's a file_path field in the input
    file_path = getattr(tool_input, "file_path", None)
    if file_path and state.is_path_confirmed(file_path):
        return False

    return True
