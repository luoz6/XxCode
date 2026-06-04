"""XxCodeTerminalUI — industrial-grade terminal interface.

Event-driven rendering powered by Rich. Interactive input via
prompt_toolkit with auto-completion, history, and custom keybindings.

All visual state is internal — no business logic, pure rendering.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.styles import merge_styles
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..agent import StreamEvent
from ..config import Config
from .completer import XxCodeCompleter
from ..ui.backend import TerminalUiBackendMixin
from .theme import (
    PROMPT_SYMBOLS,
    RISK_BORDERS,
    RISK_LABELS,
    RICH_THEME,
    TOOL_DISPLAY,
    TOOL_ICONS,
    tool_risk_level,
)
from .ui_shared import (
    YOLO_LABEL,
    TOOLBAR_SEPARATOR,
    build_session_toolbar,
    calculate_session_cost,
    format_cwd_for_display,
    normalize_permission_answer,
)

logger = logging.getLogger(__name__)

# ── prompt_toolkit style ───────────────────────────────────────────

_INPUT_STYLE = PTStyle.from_dict({
    "prompt.normal": "bold cyan",
    "prompt.yolo": "bold yellow",
    "yolo-tag": "bold yellow",
    "separator": "#888888",
    "bottom-toolbar": "bg:#1a1a1a #888888",
    "bottom-toolbar.yolo": "bg:#1a1a1a bold yellow",
})

_PICKLIST_STYLE = PTStyle.from_dict({
    "picklist.selected": "bold cyan",
    "picklist.item": "",
    "picklist.help": "#888888",
    "picklist.number": "cyan",
})


def _build_picklist_style():
    return merge_styles([_INPUT_STYLE, _PICKLIST_STYLE])


# ── Key bindings ───────────────────────────────────────────────────

def _create_keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-c")
    def _(event):
        raise KeyboardInterrupt()

    @kb.add("c-d")
    def _(event):
        raise EOFError()

    @kb.add("c-j")  # Ctrl+Enter / Ctrl+J for newline
    def _(event):
        event.current_buffer.insert_text("\n")

    return kb


# ── Toolbar builder ────────────────────────────────────────────────

def _build_toolbar(state, config: Config | None = None):
    """Build bottom-toolbar text from session state."""
    from ..api.client import get_pricing

    if config is not None and config.api_input_price_per_1k is not None:
        input_price = config.api_input_price_per_1k
    else:
        input_price = get_pricing(config.api_model if config else "claude-sonnet-4-6")["input"] / 1000
    if config is not None and config.api_output_price_per_1k is not None:
        output_price = config.api_output_price_per_1k
    else:
        output_price = get_pricing(config.api_model if config else "claude-sonnet-4-6")["output"] / 1000
    toolbar = build_session_toolbar(
        state,
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
    )
    if not toolbar:
        return ""
    if YOLO_LABEL not in toolbar:
        return [("class:bottom-toolbar", toolbar)]

    before, yolo, after = toolbar.partition(YOLO_LABEL)
    fragments: list[tuple[str, str]] = []
    if before:
        fragments.append(("class:bottom-toolbar", before))
    fragments.append(("class:bottom-toolbar.yolo", yolo))
    if after:
        fragments.append(("class:bottom-toolbar", after))
    return fragments


# ── Highlight helpers ──────────────────────────────────────────────

def _highlight_dangerous(cmd: str) -> list[tuple[str, str]]:
    """Split a shell command into safe / dangerous segments for display."""
    dangerous_patterns = ["rm -rf", "sudo rm", "> /dev/sd", "mkfs", "dd if=", "chmod 777"]
    for pat in dangerous_patterns:
        if pat in cmd:
            before, _, after = cmd.partition(pat)
            return [
                ("", before),
                ("bold red", pat),
                ("", after),
            ]
    return [("cyan", cmd)]


# ═══════════════════════════════════════════════════════════════════
# Main UI class
# ═══════════════════════════════════════════════════════════════════

class XxCodeTerminalUI(TerminalUiBackendMixin):
    """Industrial-grade terminal interface for XxCode.

    Consumes StreamEvent objects and renders them with Rich.
    Provides styled input via prompt_toolkit with autocomplete.

    All rendering state is internal — no business logic, pure presentation.
    """

    def __init__(self, config: Config | None = None):
        from ..config import get_config
        self.config = config or get_config()
        self.console = Console(soft_wrap=True, highlight=False, theme=RICH_THEME)
        self._registry = None

        # prompt_toolkit session — fall back to basic input if the
        # terminal doesn't support it (e.g. Git Bash without winpty).
        history_file = Path.home() / ".xxcode" / "history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        self.prompt_session = None
        self._perm_session = None
        self._has_prompt_toolkit = False

        try:
            self.prompt_session = PromptSession(
                history=FileHistory(str(history_file)),
                key_bindings=_create_keybindings(),
                style=_INPUT_STYLE,
                multiline=False,
                enable_history_search=True,
                completer=XxCodeCompleter(cwd=self.config.cwd),
            )
            self._perm_session = PromptSession(
                multiline=False,
                key_bindings=_create_keybindings(),
            )
            self._has_prompt_toolkit = True
        except Exception as e:
            logger.warning("prompt_toolkit unavailable: %s. Using basic input.", e)
            self.console.print(
                "[dim](Basic input mode — install winpty or use cmd.exe for full features)[/dim]",
            )
            self.console.print()

        # ── Internal rendering state ──────────────────────────────
        self._exec_context: dict[str, Any] = {
            "cwd": str(self.config.cwd),
            "config": self.config,
        }
        self._thinking_live: Live | None = None
        self._last_spinner_update: int = 0
        self.reset_for_new_session()

    def set_skill_registry(self, skill_registry) -> None:
        """Update skill registry for /<skill-name> tab completion."""
        if self.prompt_session is not None:
            self.prompt_session.completer._skill_registry = skill_registry
            self.prompt_session.completer._cwd = Path(
                self._exec_context.get("cwd", self.config.cwd)
            )

    def set_registry(self, registry: Any) -> None:
        """Wire the ToolRegistry for backfill-enriched rendering."""
        self._registry = registry

    def set_exec_context(self, context: dict[str, Any]) -> None:
        """Update execution context for backfill (cwd, etc.)."""
        self._exec_context = context
        if self.prompt_session is not None:
            self.prompt_session.completer._cwd = Path(
                context.get("cwd", self.config.cwd)
            )

    # ── Public API ─────────────────────────────────────────────────

    def render_event(self, event: StreamEvent) -> None:
        """Render a single StreamEvent to the terminal."""
        match event.type:
            case "text":
                self._flush_tool_buffer()
                self._render_text(event)
            case "thinking":
                self._render_thinking(event)
            case "tool_call":
                self._render_tool_call(event)
            case "tool_result":
                self._flush_tool_buffer()
                self._render_tool_result(event)
            case "error":
                self._flush_tool_buffer()
                self._render_error(event)
            case "cost":
                self._render_cost(event)
            case "done":
                self._flush_tool_buffer()
                self._render_done(event)
            case "permission_needed":
                self._flush_tool_buffer()
                self._render_permission_needed(event)
            case _:
                pass

    def render_welcome(self, session_id: str | None = None, skill_registry=None) -> None:
        """Render a modern professional welcome banner for XxCode.

        Layered layout: brand identity → environment info → keyboard shortcuts.
        Designed for clarity at common terminal widths (80-120 cols).
        """
        cwd = Path(self._exec_context.get("cwd", self.config.cwd))
        cwd_display = format_cwd_for_display(str(cwd), max_width=38)
        branch = _detect_git_branch(cwd)
        approval = "yolo" if self.config.yolo else "ask"
        skills = _format_skill_status(skill_registry)
        memory = "on" if self.config.auto_memory_enabled else "off"
        if self.config.auto_memory_directory:
            memory = f"on ({Path(self.config.auto_memory_directory).name})"

        # ── Brand header ──────────────────────────────────────────
        brand = Text()
        brand.append("◆  ", style="header.highlight")  # ◆
        brand.append("XxCode", style="header.brand")
        brand.append("          ", style="header.dim")
        brand.append("Coding Agent CLI", style="header.dim italic")

        # ── Environment section ────────────────────────────────────
        env_table = Table(box=None, padding=(0, 0), show_header=False, expand=True)
        env_table.add_column("label", style="header.dim", width=12, justify="right")
        env_table.add_column("value", style="header.highlight")
        env_table.add_row("model ", self.config.api_model)
        env_table.add_row("path  ", cwd_display)

        # ── Shortcuts section ──────────────────────────────────────
        shortcuts = Table(box=None, padding=(0, 1), show_header=False, expand=True)
        shortcuts.add_column("key", style="header.highlight", width=10, justify="right")
        shortcuts.add_column("desc", style="header.dim", width=22)
        shortcuts.add_column("key2", style="header.highlight", width=10, justify="right")
        shortcuts.add_column("desc2", style="header.dim", width=22)
        shortcuts.add_row(
            "/help", "Show commands",
            "Ctrl+C", "Interrupt / cancel",
        )
        shortcuts.add_row(
            "Ctrl+D", "Exit session",
            "Ctrl+J", "Insert newline",
        )

        wordmark = Text(justify="center")
        for line in (
            r"__  ____  __  ____   ___   ____   _____",
            r"\ \/ /\ \/ / / ___| / _ \ |  _ \ | ____|",
            r" \  /  \  / | |    | | | || | | ||  _|",
            r" /  \  /  \ | |___ | |_| || |_| || |___",
            r"/_/\_\/_/\_\ \____| \___/ |____/ |_____|",
        ):
            wordmark.append(line + "\n", style="header.brand")
        wordmark.append("\nXxCode", style="header.brand")
        wordmark.append("  Coding Agent CLI", style="header.highlight")
        wordmark.append("\ncalm shell, ready for work", style="header.dim italic")

        runtime = Table(box=None, padding=(0, 2), show_header=False, expand=True)
        runtime.add_column("left_label", style="header.label", width=9, no_wrap=True)
        runtime.add_column("left_value", style="header.value", ratio=1)
        runtime.add_column("right_label", style="header.label", width=9, no_wrap=True)
        runtime.add_column("right_value", style="header.value", ratio=1)
        runtime.add_row("WORKSPACE", cwd_display, "BRANCH", branch)
        runtime.add_row("MODEL", self.config.api_model, "SESSION", session_id or "new")
        runtime.add_row("APPROVAL", approval, "SKILLS", skills)
        runtime.add_row("MEMORY", memory, "WORKTREES", self.config.worktree_base_ref)

        # ── Assemble ───────────────────────────────────────────────
        body = Table(box=None, padding=(0, 1), show_header=False, expand=True)
        body.add_column(ratio=1)
        body.add_row(wordmark)
        body.add_row(Text(""))
        body.add_row(runtime)
        body.add_row(Text(""))
        body.add_row(shortcuts)

        self.console.print()
        self.console.print(
            Panel(
                body,
                title="[bold cyan] XxCode Runtime [/bold cyan]",
                subtitle="[dim]/help for commands[/dim]",
                border_style="cyan",
                box=box.ASCII_DOUBLE_HEAD,
                padding=(0, 1),
            ),
        )

    async def get_input(self, state=None) -> str | None:
        """Get user input with a styled prompt reflecting session state.

        Returns:
            Trimmed input string, or None on interrupt / EOF.
        """
        if not self._has_prompt_toolkit:
            return await self._basic_input(state)

        mode = "yolo" if _is_yolo(state) else "normal"
        symbol = PROMPT_SYMBOLS[mode]
        style_name = f"class:prompt.{mode}"
        prompt_parts = [(style_name, f"{symbol} ")]

        toolbar = _build_toolbar(state, self.config)

        try:
            text = await self.prompt_session.prompt_async(
                prompt_parts,
                bottom_toolbar=toolbar if toolbar else None,
            )
            return text.strip()
        except KeyboardInterrupt:
            return None
        except EOFError:
            return None

    async def _basic_input(self, state=None) -> str | None:
        """Fallback input when prompt_toolkit is unavailable."""
        symbol = "⚡" if _is_yolo(state) else "❯"
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, lambda: input(f"{symbol} "))
            return text.strip()
        except (KeyboardInterrupt, EOFError):
            return None

    async def pick_from_list(
        self, title: str, values: list[tuple[str, str]],
    ) -> str | None:
        """Show inline interactive selection with arrow-key navigation.

        Uses prompt_toolkit Application(full_screen=False) to render the
        list below the current cursor position — no screen takeover.
        Falls back to a numbered list when prompt_toolkit is unavailable.
        """
        if not values:
            return None

        display_values = values[:20]

        if not self._has_prompt_toolkit:
            return await self._pick_from_list_fallback(title, values)

        selected_index = [0]
        result: list[str | None] = [None]

        def _get_fragments():
            fragments: list[tuple[str, str]] = []
            for i, (_key, label) in enumerate(display_values):
                is_selected = i == selected_index[0]
                prefix = "❯ " if is_selected else "  "  # ❯
                base_style = (
                    "class:picklist.selected"
                    if is_selected
                    else "class:picklist.item"
                )
                num = f"{i + 1:2d}. "
                fragments.append(
                    (f"class:picklist.number {base_style}", f"{prefix}{num}")
                )
                fragments.append((base_style, label))
                fragments.append(("", "\n"))
            if fragments:
                last = fragments[-1]
                fragments[-1] = (last[0], last[1].rstrip("\n"))
            return fragments

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event):
            selected_index[0] = max(0, selected_index[0] - 1)

        @kb.add("down")
        @kb.add("j")
        def _down(event):
            selected_index[0] = min(
                len(display_values) - 1, selected_index[0] + 1
            )

        @kb.add("enter")
        def _enter(event):
            result[0] = display_values[selected_index[0]][0]
            event.app.exit()

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event):
            event.app.exit()

        for n in range(1, 10):

            @kb.add(str(n))
            def _quick_select(event, idx=n):
                if idx <= len(display_values):
                    selected_index[0] = idx - 1
                    result[0] = display_values[idx - 1][0]
                    event.app.exit()

        self.console.print()
        self.console.print(f"[bold]{title}[/bold]")

        from prompt_toolkit.application import Application

        style = _build_picklist_style()

        app = Application(
            layout=Layout(
                HSplit([
                    Window(height=1, char=" "),
                    Window(
                        content=FormattedTextControl(
                            text=_get_fragments,
                            key_bindings=kb,
                            focusable=True,
                            show_cursor=False,
                        ),
                        dont_extend_height=True,
                    ),
                    Window(height=1, char=" "),
                    Window(
                        height=1,
                        content=FormattedTextControl(
                            text=(  # noqa: FURB183
                                "  ↑/↓ navigate  Enter select"
                                "  Esc cancel  1-9 quick-select"
                            ),
                        ),
                        style="class:picklist.help",
                    ),
                ])
            ),
            key_bindings=kb,
            full_screen=False,
            erase_when_done=False,
            style=style,
            include_default_pygments_style=False,
        )

        await app.run_async()
        return result[0]

    async def _pick_from_list_fallback(
        self, title: str, values: list[tuple[str, str]],
    ) -> str | None:
        """Fallback: numbered list for basic input mode (no prompt_toolkit)."""
        self.console.print()
        self.console.print(f"[bold]{title}[/bold]")
        for i, (_key, label) in enumerate(values, 1):
            self.console.print(f"  [cyan]{i}[/cyan]. {label}")
        self.console.print()

        try:
            loop = asyncio.get_running_loop()
            choice = await loop.run_in_executor(
                None,
                lambda: input("Select number (Enter to cancel): "),
            )
            choice = choice.strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(values):
                    return values[idx][0]
        except (KeyboardInterrupt, EOFError):
            pass
        return None

    async def ask_permission(self, tc, dangerous: bool = False) -> str:
        """Show a permission dialog for a tool call.

        Renders a Rich Panel with risk-level coloring and detailed
        operation info, then uses prompt_toolkit for a single-key
        response.

        Args:
            tc: ToolCall with .name and .input attributes.
            dangerous: Hint from the engine (shell commands etc).

        Returns:
            One of: 'yes', 'no', 'always', 'deny_all'
        """
        risk = tool_risk_level(tc.name, tc.input)
        border = RISK_BORDERS[risk]
        label = RISK_LABELS[risk]
        display = TOOL_DISPLAY.get(tc.name, tc.name)
        icon = TOOL_ICONS.get(tc.name, "\U0001F527")  # 🔧

        # Build content lines
        content_lines: list[str] = []
        content_lines.append(f"{icon} [bold]{display}[/bold]")

        if tc.name == "run_shell":
            cmd = tc.input.get("command", "")
            # Truncate very long commands
            cmd_display = cmd if len(cmd) <= 200 else cmd[:197] + "..."
            content_lines.append("")
            content_lines.append("[bold]Command:[/bold]")
            content_lines.append(f"  [cyan]{cmd_display}[/cyan]")
        elif tc.name in ("write_file", "edit_file"):
            path = tc.input.get("file_path", "")
            content_lines.append("")
            content_lines.append(f"[bold]File:[/bold] [cyan]{path}[/cyan]")
            if "content" in tc.input:
                preview = str(tc.input.get("content", ""))
                if len(preview) > 300:
                    preview = preview[:297] + "..."
                content_lines.append("")
                content_lines.append("[bold]Content:[/bold]")
                content_lines.append(f"  [dim]{preview}[/dim]")
        elif tc.name == "read_file":
            path = tc.input.get("file_path", "")
            content_lines.append("")
            content_lines.append(f"[bold]File:[/bold] [cyan]{path}[/cyan]")
        else:
            for k, v in tc.input.items():
                val = str(v)
                if len(val) > 120:
                    val = val[:117] + "..."
                content_lines.append(f"[bold]{k}:[/bold] [dim]{val}[/dim]")

        panel_content = "\n".join(content_lines)

        self.console.print()
        self.console.print(
            Panel(
                panel_content,
                title=f"[bold {border}]{label}[/bold {border}]",
                border_style=border,
                padding=(1, 2),
            ),
        )

        # Prompt for choice
        if self._has_prompt_toolkit and self._perm_session is not None:
            choice_prompt: list[tuple[str, str]] = [
                ("bold yellow", "  ? Allow? "),
                ("dim", "[y] once  [n] deny  [a] always  [d] never  "),
            ]
            try:
                answer = await self._perm_session.prompt_async(choice_prompt)
                answer = answer.strip().lower()
            except (KeyboardInterrupt, EOFError):
                return "no"
        else:
            loop = asyncio.get_running_loop()
            try:
                answer = await loop.run_in_executor(
                    None,
                    lambda: input("  ? Allow? [y] once  [n] deny  [a] always  [d] never: "),
                )
                answer = answer.strip().lower()
            except (KeyboardInterrupt, EOFError):
                return "no"

        return normalize_permission_answer(answer)

    def render_summary(self, state) -> None:
        """Render an end-of-turn summary panel."""
        if state is None:
            return

        turns = getattr(state, "turn_count", 0)
        input_tokens = getattr(state, "total_input_tokens", 0)
        output_tokens = getattr(state, "total_output_tokens", 0)
        total_tokens = input_tokens + output_tokens
        input_price = self.config.api_input_price_per_1k
        if input_price is None:
            input_price = 0.003
        output_price = self.config.api_output_price_per_1k
        if output_price is None:
            output_price = 0.015
        total_cost = calculate_session_cost(
            input_tokens,
            output_tokens,
            input_price_per_1k=input_price,
            output_price_per_1k=output_price,
        )

        elapsed = time.time() - self._start_time if self._start_time > 0 else 0
        minutes, seconds = divmod(int(elapsed), 60)

        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Session duration", f"{minutes}m {seconds}s")
        table.add_row("Input tokens", f"{input_tokens:,}")
        table.add_row("Output tokens", f"{output_tokens:,}")
        table.add_row("Total tokens", f"{total_tokens:,}")
        table.add_row("Turns", str(turns))
        table.add_row("Total cost", f"[bold]${total_cost:.4f}[/bold]")
        if self._tool_successes + self._tool_errors > 0:
            table.add_row(
                "Tool calls",
                f"{self._tool_successes + self._tool_errors} "
                f"([green]{self._tool_successes} ok[/green], "
                f"[red]{self._tool_errors} err[/red])",
            )

        self.console.print()
        self.console.print(
            Panel(table, title="Session Summary", border_style="green", padding=(1, 2)),
        )

    def _render_task_snapshot(self, record: dict[str, Any]) -> None:
        """Render a compact task status line for multi-agent updates."""
        task_id = record.get("task_id", "")
        label = record.get("worker_label") or task_id
        status = record.get("status", "unknown")
        input_tokens = int(record.get("input_tokens", 0))
        output_tokens = int(record.get("output_tokens", 0))
        tool_use_count = int(record.get("tool_use_count", 0))
        duration_ms = int(record.get("duration_ms", 0))
        status_style = {
            "queued": "yellow",
            "running": "cyan",
            "idle": "blue",
            "completed": "green",
            "failed": "red",
            "killed": "red",
            "interrupted": "magenta",
        }.get(status, "white")
        duration_text = f"{duration_ms}ms" if duration_ms > 0 else "-"
        usage_text = f"{input_tokens}/{output_tokens} tok"
        self.console.print(
            "  "
            f"[bold {status_style}][task][/bold {status_style}] "
            f"{label} "
            f"[dim]status={status}  tools={tool_use_count}  usage={usage_text}  duration={duration_text}[/dim]",
            markup=True,
        )

    # ── Runtime hooks ─────────────────────────────────────────────

    async def prepare_runtime(self) -> None:
        """Show an animated spinner while the engine submits the request."""
        self._stop_waiting()
        spinner = Spinner("dots", text="working")
        self._waiting_live = Live(
            spinner,
            console=self.console,
            transient=True,
            refresh_per_second=8,
        )
        self._waiting_live.start()

    def shutdown(self, final_snapshot) -> None:
        """Clean up spinners and delegate to the mixin."""
        self._stop_all_spinners()
        super().shutdown(final_snapshot)

    def _stop_all_spinners(self) -> None:
        """Stop any active Live displays."""
        self._stop_waiting()
        if self._thinking_live is not None:
            self._thinking_live.stop()
            self._thinking_live = None
        self._thinking = False
        self._thinking_buffer = ""
        self._thinking_start_time = 0.0
        self._last_spinner_update = 0

    def _stop_waiting(self) -> None:
        """Stop the waiting spinner if active."""
        if self._waiting_live is not None:
            self._waiting_live.stop()
            self._waiting_live = None

    # ── Event renderers ────────────────────────────────────────────

    def _render_text(self, event: StreamEvent) -> None:
        """Stream text deltas in default terminal colour."""
        self._flush_thinking()
        self._stop_waiting()
        self._text_buffer += event.content
        try:
            self.console.print(event.content, end="", markup=False)
        except UnicodeEncodeError:
            ascii_safe = event.content.encode("ascii", errors="replace").decode("ascii")
            self.console.print(ascii_safe, end="", markup=False)

    def _render_thinking(self, event: StreamEvent) -> None:
        """Buffer thinking content; show an animated spinner with live elapsed time.

        Thinking text is accumulated rather than streamed so it can
        be rendered as a clean Panel (with timer + dynamic label)
        once the model moves on to text or tool calls.
        """
        if not self._thinking:
            self._stop_waiting()
            self._thinking = True
            self._thinking_start_time = time.time()
            self._thinking_buffer = ""
            self._last_spinner_update = 0
            spinner = Spinner("dots", text="working")
            self._thinking_live = Live(
                spinner,
                console=self.console,
                transient=True,
                refresh_per_second=8,
            )
            self._thinking_live.start()
        self._thinking_buffer += event.content
        elapsed = time.time() - self._thinking_start_time
        secs = int(elapsed)
        if secs > self._last_spinner_update and self._thinking_live is not None:
            self._last_spinner_update = secs
            self._thinking_live.update(
                Spinner("dots", text=f"working ({secs}s)")
            )

    def _flush_thinking(self) -> None:
        """Render accumulated thinking as a Panel with timer + dynamic label.

        Called when the model transitions from thinking to text, tool
        calls, or turn completion.  If no thinking was accumulated
        this is a no-op.
        """
        if not self._thinking:
            return
        self._thinking = False

        if self._thinking_live is not None:
            self._thinking_live.stop()
            self._thinking_live = None
        self._last_spinner_update = 0

        elapsed = time.time() - self._thinking_start_time

        if self._thinking_buffer.strip():
            self.console.print()
            self.console.print(
                Panel(
                    Text(self._thinking_buffer.strip(), style="dim italic"),
                    title=f"[bold cyan]working ({int(elapsed)}s)[/bold cyan]",
                    border_style="cyan",
                    padding=(0, 2),
                ),
            )

        self._thinking_buffer = ""
        self._thinking_start_time = 0.0

    def _render_tool_call(self, event: StreamEvent) -> None:
        """Buffer tool calls for grouped rendering.

        Consecutive tool_use events of the same type are collected into
        a sliding window.  When the type changes or a non-tool event
        arrives, the buffer is flushed via _flush_tool_buffer() which
        calls tool.render_grouped_tool_use() for batch display.
        """
        self._flush_thinking()
        self._stop_waiting()

        tool_name = event.content
        tool_input = event.metadata.get("input", {}) if event.metadata else {}

        # If this is a different tool type, flush the current buffer first.
        if self._tool_buffer and self._buffered_tool_name != tool_name:
            self._flush_tool_buffer()

        self._tool_buffer.append((tool_name, tool_input))
        self._buffered_tool_name = tool_name

    def _flush_tool_buffer(self) -> None:
        """Render all buffered tool calls via render_grouped_tool_use.

        Applies the backfill mechanism: each buffered tool call's raw
        input is enriched via ToolRegistry.enrich_for_render() before
        rendering, so paths/line-numbers/etc. are complete.  The original
        API parameters are never mutated.
        """
        if not self._tool_buffer:
            return

        buffered = self._tool_buffer
        tool_name = self._buffered_tool_name or ""
        self._tool_buffer = []
        self._buffered_tool_name = None

        # Try grouped rendering if the registry is wired.
        if self._registry is not None:
            tool = self._registry.get(tool_name)
            if tool is not None:
                # Backfill: enrich each input for UI display.
                enriched_inputs: list[Any] = []
                for _name, raw_input in buffered:
                    from ..tools import ToolCall
                    tc = ToolCall(id="", name=_name, input=raw_input)
                    enriched = self._registry.enrich_for_render(
                        tc, self._exec_context,
                    )
                    enriched_inputs.append(enriched)

                display_text = tool.render_grouped_tool_use(enriched_inputs)
                self._active_tool_count += len(buffered)
                self._render_grouped_display(tool_name, display_text, len(buffered))
                return

        # Fallback: render individually (no registry wired).
        for _name, raw_input in buffered:
            display = TOOL_DISPLAY.get(_name, _name)
            icon = TOOL_ICONS.get(_name, "\U0001F527")
            arg = _extract_key_arg(_name, raw_input)
            self._active_tool_count += 1
            self.console.print()
            if arg:
                self.console.print(
                    f"  [bold bright_cyan]⏺[/bold bright_cyan] {icon} [bold]{display}[/bold]([dim]{arg}[/dim])",
                    markup=True,
                )
            else:
                self.console.print(
                    f"  [bold bright_cyan]⏺[/bold bright_cyan] {icon} [bold]{display}[/bold]",
                    markup=True,
                )

    def _render_grouped_display(
        self, tool_name: str, display_text: str, count: int,
    ) -> None:
        """Render grouped tool call output.

        For single tools: compact one-line display with icon.
        For multiple tools: multi-line block with count header.
        """
        display = TOOL_DISPLAY.get(tool_name, tool_name)
        icon = TOOL_ICONS.get(tool_name, "\U0001F527")

        self.console.print()
        if count == 1:
            self.console.print(
                f"  [bold bright_cyan]⏺[/bold bright_cyan] {icon} [bold]{display}[/bold] "
                f"[dim]({display_text.split(chr(10))[-1] if chr(10) in display_text else display_text})[/dim]",
                markup=True,
            )
        else:
            self.console.print(
                f"  [bold bright_cyan]⏺[/bold bright_cyan] {icon} [bold]{display}[/bold] "
                f"[dim cyan]({count} calls)[/dim cyan]",
                markup=True,
            )
            for line in display_text.split("\n"):
                self.console.print(f"    [dim]{line}[/dim]", markup=True)

    def _render_tool_result(self, event: StreamEvent) -> None:
        """Render tool result: ✓ success or ✗ error with compact preview."""
        meta = event.metadata
        self._active_tool_count = max(0, self._active_tool_count - 1)

        if meta.get("denied"):
            self._tool_errors += 1
            self.console.print("  [bold red]✗ Denied by user[/bold red]", markup=True)
            return

        if meta.get("is_error"):
            self._tool_errors += 1
            self.console.print("  [bold red]✗ Error[/bold red]", markup=True)
            result_text = meta.get("result", "")
            if result_text:
                first_line = result_text.strip().split("\n")[0][:200]
                self.console.print(f"    [red dim]{first_line}[/red dim]", markup=True)
            return

        self._tool_successes += 1
        result_text = meta.get("result", "")
        if result_text:
            preview = result_text.strip().split("\n")[0][:300]
            if len(result_text) > 300 or "\n" in result_text:
                preview += " ..."
            self.console.print(f"  [dim green]✓[/dim green] [dim]{preview}[/dim]", markup=True)

    def _render_error(self, event: StreamEvent) -> None:
        """Render an error as a red-bordered panel."""
        self._flush_thinking()
        self._stop_waiting()
        self.console.print()
        self.console.print(
            Panel(
                event.content,
                title="Error",
                border_style="red",
                box=box.HEAVY,
            ),
        )

    def _render_cost(self, event: StreamEvent) -> None:
        """Render session cost as a dim separator line."""
        cost = event.metadata.get("cost", 0) if event.metadata else 0
        self._session_cost = cost
        self.console.print()
        self.console.print(f"  [dim]───  {event.content}[/dim]", markup=True)

    def _render_done(self, event: StreamEvent) -> None:
        """Flush thinking state, add trailing newline."""
        self._flush_thinking()
        self._stop_waiting()
        self._text_buffer = ""  # Reset for next turn
        self.console.print()

    def _render_permission_needed(self, event: StreamEvent) -> None:
        """Annotate that a permission prompt is incoming.

        The actual interaction happens via ask_permission() called by
        the REPL.  This is a visual annotation only.
        """
        self._stop_waiting()
        tc = event.metadata.get("tool_call")
        if tc is None:
            return
        risk = tool_risk_level(tc.name, tc.input)
        style = RISK_BORDERS.get(risk, "yellow")
        display = TOOL_DISPLAY.get(tc.name, tc.name)
        icon = TOOL_ICONS.get(tc.name, "\U0001F527")
        arg = _extract_key_arg(tc.name, tc.input)
        self.console.print()
        if arg:
            self.console.print(
                f"  [bold {style}]⏺ {icon} {display}({arg})[/bold {style}]",
                markup=True,
            )
        else:
            self.console.print(
                f"  [bold {style}]⏺ {icon} {display}[/bold {style}]",
                markup=True,
            )

    # ── Convenience ────────────────────────────────────────────────

    def reset_for_new_session(self) -> None:
        """Reset internal rendering counters for a fresh session."""
        self._text_buffer = ""
        self._thinking = False
        self._thinking_buffer = ""
        self._thinking_start_time = 0.0
        self._thinking_live = None
        self._last_spinner_update = 0
        self._active_tool_count = 0
        self._session_cost = 0.0
        self._start_time = time.time()
        self._tool_errors = 0
        self._tool_successes = 0
        self._tool_buffer = []
        self._buffered_tool_name = None
        self._waiting_live = None


# ── Helpers ────────────────────────────────────────────────────────


def _extract_key_arg(name: str, inp: dict) -> str:
    """Extract the single most relevant argument for compact display."""
    if name in ("read_file", "write_file", "edit_file"):
        return inp.get("file_path", "")
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:140]
    if name in ("grep_search", "glob_match"):
        return inp.get("pattern", "")
    for v in inp.values():
        if isinstance(v, str):
            return v[:80]
    return ""


def _detect_git_branch(cwd: Path) -> str:
    """Return the current git branch for the welcome banner."""
    if shutil.which("git") is None:
        return "none"
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "none"
    branch = result.stdout.strip()
    if branch:
        return branch

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "none"
    commit = result.stdout.strip()
    return f"detached:{commit}" if commit else "none"


def _format_skill_status(skill_registry) -> str:
    """Return a compact skill count for the welcome banner."""
    if skill_registry is None:
        return "off"
    try:
        count = len(skill_registry.list_all())
    except Exception:
        return "on"
    return f"{count} loaded" if count else "none"


def _is_yolo(state) -> bool:
    if state is None:
        return False
    ps = getattr(state, "permission_state", None)
    if ps is None:
        return False
    return getattr(ps, "yolo_mode", False)
