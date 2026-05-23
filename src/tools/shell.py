"""run_shell tool — execute shell commands with safety checks and limits."""

import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..compact.truncate import truncate_result
from ..security.patterns import is_dangerous
from . import Tool


class RunShellInput(BaseModel):
    command: str = Field(description="The shell command to execute")
    description: str = Field(
        default="",
        description="Brief description of what the command does (shown to user for dangerous commands)",
    )
    timeout: float | None = Field(
        default=None,
        description="Optional timeout override in seconds (max 120s, default 30s)",
    )
    workdir: str | None = Field(
        default=None,
        description="Working directory for the command. Defaults to project root.",
    )


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Execute a shell command. Output is limited to 5MB and 30s timeout. "
        "Dangerous commands (rm, sudo, mkfs, etc.) require user confirmation."
    )
    input_schema = RunShellInput

    MAX_OUTPUT_BYTES = 5 * 1024 * 1024  # 5MB
    DEFAULT_TIMEOUT = 30.0
    MAX_TIMEOUT = 120.0

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def needs_permission(self, input: RunShellInput) -> bool:
        return is_dangerous(input.command)

    async def execute(self, input: RunShellInput, context: dict[str, Any]) -> str:
        cwd = input.workdir or str(context.get("cwd", Path.cwd()))
        timeout = input.timeout if input.timeout is not None else self.DEFAULT_TIMEOUT
        timeout = min(timeout, self.MAX_TIMEOUT)

        try:
            result = subprocess.run(
                input.command,
                shell=True,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout}s.\n\nCommand: {input.command}"
        except Exception as e:
            return f"Error executing command: {e}"

        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + "[stderr]\n" + result.stderr

        # Truncate large output
        if len(output.encode("utf-8", errors="replace")) > self.MAX_OUTPUT_BYTES:
            encoded = output.encode("utf-8", errors="replace")
            half = self.MAX_OUTPUT_BYTES // 2
            start = encoded[:half].decode("utf-8", errors="replace")
            end = encoded[-half:].decode("utf-8", errors="replace")
            output = (
                f"{start}\n\n... [OUTPUT TRUNCATED: {len(encoded) - self.MAX_OUTPUT_BYTES} bytes removed] ...\n\n{end}"
            )

        exit_info = f"[Exit code: {result.returncode}]"
        return f"{exit_info}\n\n{output}" if output.strip() else exit_info
