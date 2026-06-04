"""Safety checks for skill inline shell commands."""

from __future__ import annotations

from pathlib import Path

from ..security.patterns import is_dangerous
from ..tools.BashTool.path_validation import validate_paths
from ..tools.BashTool.security import is_blocking, run_all_security_checks


def run_shell_safety_checks(command: str, cwd: Path | str) -> None:
    """Raise PermissionError when a skill inline shell command is unsafe."""
    sec_result = run_all_security_checks(command)
    if is_blocking(sec_result):
        findings = "; ".join(desc for _, desc in sec_result.findings[:3])
        raise PermissionError(
            f"Inline shell blocked by security checks: {findings}"
        )

    if is_dangerous(command):
        raise PermissionError(
            "Inline shell command matches a dangerous pattern."
        )

    valid, invalid_paths = validate_paths(command, str(cwd))
    if not valid:
        raise PermissionError(
            "Inline shell command references paths outside the workspace: "
            + ", ".join(invalid_paths[:5])
        )


__all__ = ["run_shell_safety_checks"]
