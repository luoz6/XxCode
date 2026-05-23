"""Rich terminal renderer for the coding agent output."""

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from ..agent import StreamEvent

# Tool icons
TOOL_ICONS: dict[str, str] = {
    "read_file": "\U0001F4D6",   # 📖
    "write_file": "✏️",  # ✏️
    "edit_file": "✏️",  # ✏️
    "grep_search": "\U0001F50D",  # 🔍
    "glob_match": "\U0001F50D",  # 🔍
    "run_shell": "\U0001F4BB",    # 💻
}


class Renderer:
    """Renders agent stream events to the terminal using Rich."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._current_text = ""
        self._tool_results_shown = 0

    def render_event(self, event: StreamEvent) -> None:
        """Render a single stream event."""
        match event.type:
            case "text":
                self._render_text(event)

            case "tool_call":
                self._render_tool_call(event)

            case "tool_result":
                self._render_tool_result(event)

            case "error":
                self._render_error(event)

            case "cost":
                self._render_cost(event)

            case "done":
                self._render_done(event)

            case "permission_needed":
                self._render_permission_needed(event)

            case _:
                pass  # Unknown event type, skip

    def _render_text(self, event: StreamEvent) -> None:
        """Stream text to the console character by character."""
        self.console.print(event.content, end="", style="green", markup=False)

    def _render_tool_call(self, event: StreamEvent) -> None:
        """Render a tool call indicator."""
        icon = TOOL_ICONS.get(event.content, "\U0001F527")  # 🔧 fallback
        input_preview = str(event.metadata.get("input", {}))
        if len(input_preview) > 80:
            input_preview = input_preview[:80] + "..."

        self.console.print()
        self.console.print(
            f"  {icon} [bold yellow]{event.content}[/bold yellow] {input_preview}",
            markup=True,
        )

    def _render_tool_result(self, event: StreamEvent) -> None:
        """Render a tool result (shows first 500 chars, model sees full)."""
        self._tool_results_shown += 1
        result_text = event.metadata.get("result", "")

        if event.metadata.get("denied"):
            self.console.print("  [bold red]✗ Denied[/bold red]", markup=True)
            return

        if event.metadata.get("is_error"):
            self.console.print(f"  [bold red]✗ Error[/bold red]", markup=True)
            if result_text:
                self.console.print(f"    {result_text[:200]}", style="red", markup=False)
            return

        # Show truncated preview
        if result_text:
            preview = result_text[:300]
            if len(result_text) > 300:
                preview += f"\n  ... (full result sent to model)"
            self.console.print(f"    {preview}", style="dim", markup=False)

    def _render_error(self, event: StreamEvent) -> None:
        """Render an error message."""
        self.console.print()
        self.console.print(
            Panel(event.content, title="Error", border_style="red"),
        )

    def _render_cost(self, event: StreamEvent) -> None:
        """Render session cost."""
        self.console.print()
        self.console.print(
            f"  [dim]Cost: [bold]{event.content}[/bold][/dim]",
            markup=True,
        )

    def _render_done(self, event: StreamEvent) -> None:
        """Signal end of agent response."""
        self.console.print()

    def _render_permission_needed(self, event: StreamEvent) -> None:
        """Indicate that user permission is needed."""
        tc = event.metadata.get("tool_call")
        if tc:
            self.console.print(
                f"  [bold yellow]⚠ Need permission for: {tc.name}[/bold yellow]",
                markup=True,
            )


# Singleton
_renderer: Renderer | None = None


def get_renderer() -> Renderer:
    global _renderer
    if _renderer is None:
        _renderer = Renderer()
    return _renderer
