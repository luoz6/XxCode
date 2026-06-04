"""Permission checking pipeline."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SHELL_RULE_PREFIX = "Bash("


@dataclass
class PermissionState:
    """Tracks which paths and tools have been approved in this session."""

    confirmed_paths: set[str] = field(default_factory=set)
    confirmed_tools: set[str] = field(default_factory=set)
    confirmed_command_rules: set[str] = field(default_factory=set)
    yolo_mode: bool = False

    def is_path_confirmed(self, path: str) -> bool:
        """Check if a path (or any ancestor) has been confirmed."""
        if self.yolo_mode:
            return True
        p = Path(path).resolve()
        normalized = str(p).replace("\\", "/")
        parts = normalized.split("/")
        # Check prefixes — normalize stored paths too (they may have
        # backslashes from older sessions or cross-platform state files).
        confirmed_normalized = {cp.replace("\\", "/") for cp in self.confirmed_paths}
        for i in range(len(parts) + 1):
            prefix = "/".join(parts[:i]) or "/"
            if prefix in confirmed_normalized:
                return True
        return False

    def confirm_path(self, path: str) -> None:
        """Mark a path as confirmed."""
        p = Path(path).resolve()
        self.confirmed_paths.add(str(p).replace("\\", "/"))

    def is_tool_confirmed(self, tool_name: str) -> bool:
        """Check if a tool invocation has been pre-approved."""
        if self.yolo_mode:
            return True
        return tool_name in self.confirmed_tools

    def confirm_tool(self, tool_name: str) -> None:
        """Mark a tool as pre-approved for this session."""
        self.confirmed_tools.add(tool_name)

    def is_command_rule_confirmed(self, command: str) -> bool:
        """Check if a shell command matches a saved Bash prefix rule."""
        if self.yolo_mode:
            return True
        try:
            from xxcode.tools.BashTool.permissions import get_simple_command_prefix
        except Exception:
            return False

        prefix = get_simple_command_prefix(command)
        if not prefix:
            return False
        try:
            from xxcode.tools.BashTool.permissions import analyze_command_permissions
            perm = analyze_command_permissions(command)
            if perm.needs_user_decision:
                return False
        except Exception:
            return False
        return self._command_rule_for_prefix(prefix) in self.confirmed_command_rules

    def confirm_command_prefix(self, prefix: str) -> None:
        """Mark a shell command prefix as pre-approved."""
        normalized = prefix.strip()
        if normalized:
            self.confirmed_command_rules.add(self._command_rule_for_prefix(normalized))

    @staticmethod
    def _command_rule_for_prefix(prefix: str) -> str:
        return f"{SHELL_RULE_PREFIX}{prefix}:*)"

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict for session persistence."""
        return {
            "confirmed_paths": sorted(self.confirmed_paths),
            "confirmed_tools": sorted(self.confirmed_tools),
            "confirmed_command_rules": sorted(self.confirmed_command_rules),
            "yolo_mode": self.yolo_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PermissionState":
        """Deserialize from a dict produced by to_dict()."""
        return cls(
            confirmed_paths=set(data.get("confirmed_paths", [])),
            confirmed_tools=set(data.get("confirmed_tools", [])),
            confirmed_command_rules=set(data.get("confirmed_command_rules", [])),
            yolo_mode=data.get("yolo_mode", False),
        )


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
