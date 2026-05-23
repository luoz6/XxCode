"""REPL (Read-Eval-Print Loop) using prompt_toolkit for interactive input."""

import asyncio
import logging
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

logger = logging.getLogger(__name__)

REPL_STYLE = Style.from_dict({
    "prompt": "bold cyan",
    "separator": "dim",
})


def create_repl_session() -> PromptSession:
    """Create a prompt_toolkit session with history and key bindings."""

    history_file = Path.home() / ".xxcode" / ".repl_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _(event):
        """Ctrl+C: raise KeyboardInterrupt for the agent loop to handle."""
        raise KeyboardInterrupt()

    session = PromptSession(
        history=FileHistory(str(history_file)),
        key_bindings=bindings,
        style=REPL_STYLE,
        multiline=False,
        enable_history_search=True,
    )

    return session


def get_user_input(session: PromptSession) -> str | None:
    """Get one line of input from the user.

    Returns:
        User input string, or None on EOF (Ctrl+D).
    """
    try:
        text = session.prompt([("class:prompt", "> ")], multiline=False)
        return text.strip()
    except KeyboardInterrupt:
        # First Ctrl+C — return empty (will be handled by caller)
        return None
    except EOFError:
        return None


async def run_repl(agent, renderer, config) -> None:
    """Run the interactive REPL loop.

    Args:
        agent: CodingAgent instance.
        renderer: Renderer instance for output.
        config: Config instance.
    """
    from rich.console import Console

    console = Console()

    console.print()
    console.print("[bold]XxCode[/bold] — type [cyan]/help[/cyan] for commands, [cyan]Ctrl+C[/cyan] to interrupt, [cyan]Ctrl+D[/cyan] to exit")
    console.print()

    session = create_repl_session()
    agent_state = None
    ctrl_c_count = 0

    while True:
        try:
            user_input = get_user_input(session)
        except KeyboardInterrupt:
            ctrl_c_count += 1
            if ctrl_c_count >= 2:
                console.print("\n[dim]Second Ctrl+C — exiting.[/dim]")
                break
            if agent_state is not None:
                agent.abort()
                console.print("\n[dim]Aborting current task... (press Ctrl+C again to exit)[/dim]")
            continue

        ctrl_c_count = 0  # Reset on successful input

        if user_input is None:
            # Ctrl+C during input
            console.print("\n[dim](Press Ctrl+C again to exit, or type a new prompt)[/dim]")
            continue

        if not user_input:
            continue

        # Handle slash commands
        if user_input.startswith("/"):
            cmd = user_input[1:].strip().lower()

            if cmd in ("q", "quit", "exit"):
                break
            elif cmd == "clear":
                console.clear()
                agent_state = None
                console.print("[dim]Session cleared.[/dim]")
                continue
            elif cmd == "cost":
                if agent_state:
                    cost = agent._calculate_cost(agent_state)
                    console.print(f"Total cost: [bold]${cost:.6f}[/bold]")
                else:
                    console.print("No active session.")
                continue
            elif cmd in ("compact", "compress"):
                if agent_state:
                    agent_state = await agent._compact(agent_state)
                    console.print("[dim]Context compressed.[/dim]")
                else:
                    console.print("No active session.")
                continue
            elif cmd == "yolo" or cmd.startswith("yolo "):
                new_state = "on"
                if agent_state:
                    agent_state.permission_state.yolo_mode = not agent_state.permission_state.yolo_mode
                    new_state = "ON" if agent_state.permission_state.yolo_mode else "OFF"
                console.print(f"YOLO mode: [bold]{new_state}[/bold]")
                continue
            elif cmd == "help":
                console.print("""
[bold]Commands:[/bold]
  [cyan]/help[/cyan]      — Show this help
  [cyan]/clear[/cyan]     — Clear session history
  [cyan]/cost[/cyan]      — Show session cost
  [cyan]/compact[/cyan]   — Manually compress context
  [cyan]/yolo[/cyan]      — Toggle YOLO mode (skip all permission prompts)
  [cyan]/quit[/cyan]      — Exit
""")
                continue
            else:
                console.print(f"[red]Unknown command: {user_input}. Try /help[/red]")
                continue

        # Execute the agent
        console.print()
        try:
            state_to_pass = agent_state if agent_state is not None else agent._last_state

            async for event in agent.chat(user_input, state_to_pass):
                renderer.render_event(event)

            # Capture state from agent for the next turn
            agent_state = agent._last_state

        except Exception as e:
            logger.exception("Error in agent loop")
            console.print(f"\n[bold red]Error:[/bold red] {e}")
            agent.reset()

        console.print()  # Blank line between turns
