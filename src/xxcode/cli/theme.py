"""Visual theme constants, icons, and risk-level helpers."""

from rich.theme import Theme


PROMPT_SYMBOLS = {
    "normal": "\u276f",
    "yolo": "\u26a1",
}


TOOL_ICONS: dict[str, str] = {
    "read_file": "\U0001F4D6",
    "write_file": "\u270d\ufe0f",
    "edit_file": "\U0001F4DD",
    "grep_search": "\U0001F50D",
    "glob_match": "\U0001F50E",
    "run_shell": "\U0001F4BB",
}

TOOL_DISPLAY: dict[str, str] = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "grep_search": "Grep",
    "glob_match": "Glob",
    "run_shell": "Bash",
}


def tool_risk_level(tool_name: str, tool_input: dict) -> str:
    """Return 'low', 'medium', or 'high' for a tool call."""
    if tool_name in ("read_file", "grep_search", "glob_match"):
        return "low"
    if tool_name == "run_shell":
        cmd = tool_input.get("command", "")
        if any(
            danger in cmd
            for danger in (
                "rm -rf",
                "sudo rm",
                "format",
                "mkfs",
                "dd if=",
                "> /dev/sd",
                "chmod 777",
            )
        ):
            return "high"
        if any(
            danger in cmd
            for danger in ("rm ", "sudo", "chmod", "chown", "kill ", "pip", "npm install -g")
        ):
            return "medium"
        return "low"
    if tool_name in ("write_file", "edit_file"):
        path = tool_input.get("file_path", "")
        if any(
            danger in path
            for danger in (
                "/etc/",
                "/proc/",
                "/sys/",
                "/dev/",
                "C:\\Windows\\",
                "C:\\windows\\",
            )
        ):
            return "high"
        return "medium"
    return "low"


RISK_STYLES: dict[str, str] = {
    "low": "yellow",
    "medium": "orange1",
    "high": "bold red",
}

RISK_BORDERS: dict[str, str] = {
    "low": "yellow",
    "medium": "orange1",
    "high": "red",
}

RISK_LABELS: dict[str, str] = {
    "low": "Low Risk",
    "medium": "Medium Risk - Review Carefully",
    "high": "High Risk - Dangerous Operation",
}


RICH_THEME = Theme(
    {
        "prompt.normal": "bold cyan",
        "prompt.yolo": "bold yellow",
        "header.brand": "bold bright_cyan",
        "header.dim": "dim",
        "header.highlight": "bold cyan",
        "header.label": "bold white",
        "header.value": "cyan",
        "thinking": "dim italic",
        "tool.name": "bold bright_cyan",
        "tool.args": "dim",
        "tool.result": "dim",
        "tool.success": "green",
        "tool.error": "bold red",
        "cost": "dim",
        "separator": "dim",
        "info": "dim",
        "warning": "bold yellow",
        "danger": "bold red",
        "heading": "bold white",
    }
)
