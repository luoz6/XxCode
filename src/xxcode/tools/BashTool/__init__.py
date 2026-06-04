"""BashTool — the system's most complex tool.

Executes shell commands with a multi-layer security architecture:
  1. Security validation   — 23 checks before anything else
  2. Permission analysis   — 5-step layered permission check
  3. Sed validation        — whitelist-based sed safety
  4. Path validation       — workspace boundary enforcement
  5. Sandbox execution     — optional macOS/Linux sandbox
  6. Background management — long-running task support
  7. Exit code semantics   — command-specific interpretation

Port of Claude Code's BashTool (18 source files in TypeScript).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import Tool
from . import background as _bg
from . import command_semantics as _cs
from . import path_validation as _pv
from . import permissions as _perm
from . import sandbox as _sb
from . import security as _sec
from . import sed_validation as _sv

logger = logging.getLogger(__name__)


# ── Input schema ──────────────────────────────────────────────────────

class BashInput(BaseModel):
    """Input schema for the BashTool — matches the TypeScript version."""

    command: str = Field(
        description="The shell command to execute",
    )
    timeout: int | None = Field(
        default=None,
        description="Optional timeout in milliseconds",
    )
    description: str = Field(
        default="",
        description="Brief activity description for UI display",
    )
    run_in_background: bool = Field(
        default=False,
        description="If True, execute asynchronously and return immediately",
    )
    dangerouslyDisableSandbox: bool = Field(
        default=False,
        description="If True, skip sandbox isolation (requires policy approval)",
    )


# ── BashTool class ────────────────────────────────────────────────────

class BashTool(Tool):
    """Execute shell commands with comprehensive security layers.

    Security pipeline (in order):
      Stage A: Security validation — 23 checks, blocking patterns
      Stage B: Permission analysis — 5-step layered decision
      Stage C: Sed validation — whitelist for sed expressions
      Stage D: Path validation — workspace boundary check
      Stage E: Sandbox wrapper — platform isolation
      Stage F: Execution — subprocess with timeout
      Stage G: Result interpretation — exit code semantics
    """

    name = "run_shell"
    description = (
        "Execute a shell command in the project environment. "
        "Output is limited to 5MB. Use BashTool for running shell commands, "
        "scripts, build tools, and system utilities.\n\n"
        "Security: Dangerous commands are blocked or require user confirmation. "
        "Use 'description' to explain what the command does (shown to user). "
        "Use 'run_in_background: true' for long-running commands. "
        "Use 'dangerouslyDisableSandbox: true' only when sandbox isolation "
        "prevents legitimate operations."
    )
    input_schema = BashInput
    aliases = ["bash", "shell", "Bash"]

    # ── Constants ─────────────────────────────────────────────────

    MAX_OUTPUT_BYTES = 5 * 1024 * 1024  # 5MB
    DEFAULT_TIMEOUT_SECONDS = 30.0
    MAX_TIMEOUT_SECONDS = 120.0

    _is_read_only = False
    _is_concurrency_safe = False
    _is_destructive = True
    _max_output_chars = 100_000  # Shell output can be verbose

    def __init__(self):
        self._sandbox_manager = _sb.SandboxManager()
        self._background_manager: _bg.BackgroundManager | None = None

    # ── Security policy overrides ─────────────────────────────────

    def needs_permission(self, input: BashInput) -> bool:
        """Determine if this command requires user permission.

        Uses the full multi-layer permission analysis.
        """
        # First check: hard-blocked commands always need permission.
        if _pv.is_absolute_destroy(input.command):
            return True

        # Run security checks.
        sec_result = _sec.run_all_security_checks(input.command)
        if _sec.is_blocking(sec_result):
            return True

        # Check sed commands specifically.
        stripped = input.command.strip()
        if stripped and stripped.split()[0].lower() == "sed":
            sed_result = _sv.validate_sed_command(input.command)
            if not sed_result.safe:
                return True

        # Full permission analysis.
        perm_result = _perm.analyze_command_permissions(input.command)
        return perm_result.needs_user_decision

    def is_destructive(self, input: BaseModel | None = None) -> bool:
        """Input-aware destructiveness — depends on the command.

        Read-only commands (ls, cat, grep, etc.) are NOT destructive.
        Write commands (rm, mv, etc.) ARE destructive.
        """
        if input is not None and hasattr(input, "command"):
            from .command_semantics import classify_bash_command
            category = classify_bash_command(input.command)
            return category in ("write", "unknown")
        return True  # Conservative default for unknown calls

    def is_read_only(self, input: BaseModel | None = None) -> bool:
        """Input-aware read-only check — safe commands are read-only."""
        if input is not None and hasattr(input, "command"):
            from ...security.classifier import classify_command, CommandClass
            cr = classify_command(input.command)
            return cr.command_class == CommandClass.SAFE
        return False  # Conservative: assume not read-only

    def has_command_classifier(self) -> bool:
        """BashTool has the command classifier for speculative auto-approval."""
        return True

    def supports_sibling_abort(self) -> bool:
        """Bash tools cascade-fail: when one fails, abort all others."""
        return True

    # ── UI rendering ──────────────────────────────────────────────

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, BashInput)
        cmd = input.command
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"Bash {cmd}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"Bash failed: {content[:150]}"
        if content.startswith("[Exit code:"):
            return content.split("\n")[0]
        first_line = content.strip().split("\n")[0][:150]
        return first_line if first_line else "(empty output)"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        cmds = [
            inp.command[:60] for inp in inputs  # type: ignore[union-attr]
            if hasattr(inp, "command")
        ]
        return f"Bash ({len(inputs)} commands): {'; '.join(cmds[:3])}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Enrich with classified command category for UI folding."""
        assert isinstance(input, BashInput)
        category = _cs.classify_bash_command(input.command)
        return BashInput(
            command=input.command,
            timeout=input.timeout,
            description=input.description or category,
            run_in_background=input.run_in_background,
            dangerouslyDisableSandbox=input.dangerouslyDisableSandbox,
        )

    # ── Core execution ────────────────────────────────────────────

    async def execute(self, input: BashInput, context: dict[str, Any]) -> str:
        """Stage F: Execute the shell command through all security layers.

        Pipeline: security → permission → sed → path → sandbox → execute → interpret.
        """
        command = input.command
        cwd = context.get("cwd", str(Path.cwd()))
        timeout_seconds = (
            (input.timeout / 1000.0) if input.timeout is not None
            else self.DEFAULT_TIMEOUT_SECONDS
        )
        timeout_seconds = min(timeout_seconds, self.MAX_TIMEOUT_SECONDS)

        # ── Stage A: Security validation ──────────────────────────
        sec_result = _sec.run_all_security_checks(command)
        if _sec.is_blocking(sec_result):
            findings = "; ".join(
                desc for _, desc in sec_result.findings[:3]
            )
            return (
                f"<tool_use_error>\n"
                f"Command blocked by security checks:\n"
                f"  {findings}\n"
                f"The command contains patterns that are unsafe. "
                f"Use an alternative approach.\n"
                f"</tool_use_error>"
            )

        # ── Stage B: Permission analysis (informational) ──────────
        perm_result = _perm.analyze_command_permissions(command)
        if perm_result.parse_result == _perm.ParseResult.TOO_COMPLEX:
            logger.warning(
                "Command too complex for analysis: %s", command[:100],
            )

        # ── Stage C: Sed validation ───────────────────────────────
        stripped = command.strip()
        if stripped and stripped.split()[0].lower() == "sed":
            sed_result = _sv.validate_sed_command(command)
            if not sed_result.safe:
                return (
                    f"<tool_use_error>\n"
                    f"sed command not auto-approved: {sed_result.reason}\n"
                    f"The sed expression uses features that require user review. "
                    f"Use read_file + write_file/edit_file instead, or simplify "
                    f"the sed expression to use only safe patterns.\n"
                    f"</tool_use_error>"
                )

        # ── Stage D: Path validation ──────────────────────────────
        if not _pv.is_absolute_destroy(command):
            valid, invalid_paths = _pv.validate_paths(command, cwd)
            if not valid:
                return (
                    f"<tool_use_error>\n"
                    f"Path validation failed: the command references files "
                    f"outside the workspace.\n"
                    f"  Blocked paths: {', '.join(invalid_paths[:5])}\n"
                    f"  Workspace: {cwd}\n"
                    f"Use paths within the project workspace, or explicitly "
                    f"acknowledge the out-of-workspace access.\n"
                    f"</tool_use_error>"
                )

        # ── Stage E: Background execution ─────────────────────────
        if input.run_in_background:
            return await self._execute_background(command, input.description, cwd, timeout_seconds)

        # ── Stage F: Sandbox + execute ────────────────────────────
        use_sandbox = _sb.should_use_sandbox(
            command,
            self._sandbox_manager,
            dangerously_disable_sandbox=input.dangerouslyDisableSandbox,
        )

        if use_sandbox:
            sandbox_config = _sb.SandboxConfig(
                allow_write=[cwd],
                allow_read=[cwd],
                allow_network=False,
            )
            cmd_list = self._sandbox_manager.get_sandbox_command(
                command, sandbox_config, cwd,
            )
            exit_code, output = await self._run_argv_command(
                cmd_list,
                cwd,
                timeout_seconds,
                command,
            )
        else:
            exit_code, output = await self._run_command(command, cwd, timeout_seconds)

        # ── Stage G: Result interpretation ────────────────────────
        output = self._truncate_output(output)
        exit_info = _cs.format_exit_code(command, exit_code)
        return f"{exit_info}\n\n{output}" if output.strip() else exit_info

    async def _run_command(
        self, command: str, cwd: str, timeout_seconds: float,
    ) -> tuple[int, str]:
        """Execute a command using subprocess."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                **_bg._process_group_kwargs(),
            )
            return await self._communicate_process(proc, timeout_seconds, command)

        except asyncio.CancelledError:
            if proc is not None:
                try:
                    await self._terminate_and_reap(proc)
                except Exception:
                    logger.debug("Failed to terminate cancelled shell process", exc_info=True)
            raise
        except Exception as exc:
            return -1, f"Error executing command: {exc}"

    async def _run_argv_command(
        self,
        cmd_list: list[str],
        cwd: str,
        timeout_seconds: float,
        command: str,
    ) -> tuple[int, str]:
        """Execute an argv command directly without shell re-tokenization."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                **_bg._process_group_kwargs(),
            )
            return await self._communicate_process(proc, timeout_seconds, command)
        except asyncio.CancelledError:
            if proc is not None:
                try:
                    await self._terminate_and_reap(proc)
                except Exception:
                    logger.debug("Failed to terminate cancelled sandbox process", exc_info=True)
            raise
        except Exception as exc:
            return -1, f"Error executing command: {exc}"

    async def _communicate_process(
        self,
        proc: asyncio.subprocess.Process,
        timeout_seconds: float,
        command: str,
    ) -> tuple[int, str]:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._terminate_and_reap(proc)
            return -1, f"Error: Command timed out after {timeout_seconds}s.\n\nCommand: {command}"

        exit_code = proc.returncode or 0
        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            output += ("\n" if output else "") + "[stderr]\n" + stderr.decode("utf-8", errors="replace")
        return exit_code, output

    async def _terminate_and_reap(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        await _bg._terminate_process_tree(proc)
        await proc.wait()

    async def _execute_background(
        self, command: str, description: str, cwd: str, timeout_seconds: float,
    ) -> str:
        """Launch a command in the background."""
        if self._background_manager is None:
            # Init background manager lazily.
            session_dir = str(Path.home() / ".xxcode" / "sessions")
            self._background_manager = _bg.BackgroundManager(session_dir)

        try:
            task = await self._background_manager.start_background(
                command=command,
                description=description,
                cwd=cwd,
                timeout=timeout_seconds if timeout_seconds < self.MAX_TIMEOUT_SECONDS else None,
            )
            return (
                f"Command launched in background.\n"
                f"Task ID: {task.task_id}\n"
                f"Description: {description or command[:80]}\n\n"
                f"Output file: {task.output_path}\n"
                f"Error file:  {task.error_path}\n\n"
                f"Use read_file to check the output file for results."
            )
        except RuntimeError as exc:
            return (
                f"Could not start background task: {exc}\n"
                f"Current active tasks: {self._background_manager.active_count}"
            )

    def _truncate_output(self, output: str) -> str:
        """Truncate large output to MAX_OUTPUT_BYTES."""
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) <= self.MAX_OUTPUT_BYTES:
            return output
        half = self.MAX_OUTPUT_BYTES // 2
        start = encoded[:half].decode("utf-8", errors="replace")
        end = encoded[-half:].decode("utf-8", errors="replace")
        removed = len(encoded) - self.MAX_OUTPUT_BYTES
        return (
            f"{start}\n\n"
            f"... [OUTPUT TRUNCATED: {removed} bytes removed] ...\n\n"
            f"{end}"
        )
