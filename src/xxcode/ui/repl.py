"""REPL session orchestrator.

Drives the QueryEngine event loop and delegates all rendering to the UI.
Handles slash-command dispatch and session persistence.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from ..cli.commands import iter_command_help_rows
from ..skills import resolve_skill_context_cwd

logger = logging.getLogger(__name__)


def _report_persistence_error(console, action: str, exc: Exception) -> None:
    """Surface session persistence failures without terminating the REPL."""
    console.print(
        "  [bold red]Session persistence error:[/bold red] "
        f"[dim]{action}: {exc}[/dim]"
    )


def _cmd_help(console, *, skill_registry=None, cwd=None) -> None:
    """Show help with a Rich table."""
    from rich.table import Table

    table = Table(title="Commands", box=None, title_style="bold cyan", padding=(0, 2))
    table.add_column("Command", style="bold cyan", width=14, no_wrap=True)
    table.add_column("Description", style="dim")

    for command, description in iter_command_help_rows(
        skill_registry=skill_registry,
        cwd=cwd,
    ):
        table.add_row(command, description)

    console.print()
    console.print(table)
    console.print()


def _cmd_cost(console, agent_state) -> None:
    """Show session cost."""
    if agent_state is None:
        console.print("  [dim]No active session.[/dim]")
        return

    input_tokens = agent_state.total_input_tokens
    output_tokens = agent_state.total_output_tokens
    input_cost = (input_tokens / 1000) * 0.003
    output_cost = (output_tokens / 1000) * 0.015
    total = input_cost + output_cost

    from rich.table import Table

    table = Table(
        title="Session Cost",
        box=None,
        title_style="bold green",
        padding=(0, 2),
    )
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")

    table.add_row("Input tokens", f"{input_tokens:,}")
    table.add_row("Output tokens", f"{output_tokens:,}")
    table.add_row("Total tokens", f"{input_tokens + output_tokens:,}")
    table.add_row("Turns", str(agent_state.turn_count))
    table.add_row("", "")
    table.add_row("Input cost  ($0.003/1K)", f"${input_cost:.4f}")
    table.add_row("Output cost ($0.015/1K)", f"${output_cost:.4f}")
    table.add_row("Total cost", f"[bold]${total:.4f}[/bold]")

    console.print()
    console.print(table)
    console.print()


def _cmd_tokens(console, agent_state) -> None:
    """Show token breakdown (alias for /cost)."""
    _cmd_cost(console, agent_state)


def _cmd_sessions(console, config) -> None:
    """List saved sessions."""
    from datetime import datetime
    from rich.table import Table

    from .session import SessionStore

    store = SessionStore(config.session_dir)
    sessions = store.list_sessions()

    if not sessions:
        console.print("  [dim]No saved sessions.[/dim]")
        return

    table = Table(title="Saved Sessions", box=None, title_style="bold cyan", padding=(0, 2))
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Messages", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Last Updated", style="dim")

    for session in sessions[:20]:
        dt = datetime.fromtimestamp(session.last_updated).strftime("%m-%d %H:%M")
        table.add_row(
            session.session_id,
            str(session.message_count),
            str(session.turn_count),
            dt,
        )

    console.print()
    console.print(table)
    console.print("  [dim]恢复: /resume <session-id>  或  xxcode --resume <session-id>[/dim]")
    console.print()


async def run_repl(
    engine,
    ui,
    config,
    initial_state=None,
    session_id=None,
    skill_registry=None,
) -> None:
    """Run the interactive REPL loop."""
    from rich.panel import Panel

    from .session import SessionStore
    from .runtime import UiRuntime

    console = ui.console

    if session_id is None:
        session_id = uuid.uuid4().hex[:12]

    if hasattr(engine, "core_engine") and hasattr(engine.core_engine, "_registry"):
        ui.set_registry(engine.core_engine._registry)

    def _refresh_exec_context():
        current_cwd = resolve_skill_context_cwd(
            config.cwd,
            getattr(getattr(engine, "core_engine", None), "_context", {}),
        )
        ui.set_exec_context({"cwd": str(current_cwd), "config": config})
        return current_cwd

    current_cwd = _refresh_exec_context()
    ui.render_welcome(session_id=session_id, skill_registry=skill_registry)

    if initial_state is not None:
        agent_state = initial_state
        engine._last_state = initial_state
    else:
        agent_state = None

    store = SessionStore(config.session_dir)
    ctrl_c_count = 0
    ui_runtime = UiRuntime(engine=engine, ui=ui)

    def _save_session_snapshot(current_state) -> bool:
        if not current_state:
            return False
        try:
            store.save(session_id, current_state.messages)
            store.save_state_with_recovery(
                session_id,
                current_state,
                engine.core_engine.export_skill_recovery_snapshot(),
                engine.core_engine.task_runtime.export_snapshot(),
            )
        except Exception as exc:
            logger.exception("Session persistence failed")
            _report_persistence_error(console, "failed to save session state", exc)
            return False
        return True

    try:
        while True:
            current_cwd = _refresh_exec_context()
            try:
                user_input = await ui.get_input(agent_state)
            except KeyboardInterrupt:
                ctrl_c_count += 1
                if ctrl_c_count >= 2:
                    console.print("\n[dim]Second Ctrl+C - exiting.[/dim]")
                    break
                if agent_state is not None:
                    engine.abort()
                    console.print("\n[dim]Aborting... (Ctrl+C again to exit)[/dim]")
                continue

            ctrl_c_count = 0

            if user_input is None:
                console.print()
                continue

            if not user_input:
                continue

            if user_input.startswith("/"):
                cmd = user_input[1:].strip().lower()
                cmd_name = cmd.split()[0] if cmd else ""

                if cmd in ("q", "quit", "exit"):
                    console.print("[dim]Goodbye.[/dim]")
                    break

                if cmd == "clear":
                    console.clear()
                    agent_state = None
                    engine.reset()
                    await engine.clear_mcp()
                    ui.reset_for_new_session()
                    ui_runtime = UiRuntime(engine=engine, ui=ui)
                    session_id = uuid.uuid4().hex[:12]
                    console.print(
                        f"[dim]Session cleared. New ID: [cyan]{session_id}[/cyan][/dim]"
                    )
                    continue

                if cmd in ("s", "save"):
                    if agent_state:
                        if _save_session_snapshot(agent_state):
                            console.print(
                                f"[dim]Saved [cyan]{session_id}[/cyan] "
                                f"({len(agent_state.messages)} msgs, "
                                f"{agent_state.turn_count} turns)[/dim]"
                            )
                    else:
                        console.print("  [dim]No active session.[/dim]")
                    continue

                if cmd == "cost":
                    _cmd_cost(console, agent_state)
                    continue

                if cmd == "tokens":
                    _cmd_tokens(console, agent_state)
                    continue

                if cmd == "sessions":
                    _cmd_sessions(console, config)
                    continue

                if cmd_name == "resume":
                    target_id = cmd.split()[1] if len(cmd.split()) > 1 else ""
                    if not target_id:
                        sessions = store.list_sessions()
                        if not sessions:
                            console.print("  [dim]No saved sessions.[/dim]")
                            continue
                        values = [
                            (
                                s.session_id,
                                f"{s.session_id}  ({s.message_count} msgs, {s.turn_count} turns)",
                            )
                            for s in sessions[:20]
                        ]
                        target_id = await ui.pick_from_list(
                            title="Resume Session",
                            values=values,
                        )
                        if not target_id:
                            continue
                    resumed_state = store.load_state(target_id)
                    if resumed_state is None:
                        console.print(f"  [red]会话 {target_id} 未找到。[/red]")
                        continue
                    # 恢复 recovery 数据（镜像 main.py --resume 逻辑）
                    recovery = store.load_skill_recovery(target_id)
                    if recovery is not None:
                        engine.core_engine.import_skill_recovery_snapshot(recovery)
                    task_snapshot = store.load_task_runtime_snapshot(target_id)
                    engine.core_engine.task_runtime.import_snapshot(task_snapshot)
                    # 切换会话
                    ui.reset_for_new_session()
                    ui_runtime = UiRuntime(engine=engine, ui=ui)
                    session_id = target_id
                    agent_state = resumed_state
                    engine._last_state = resumed_state
                    console.print(
                        f"  [dim]已恢复 [cyan]{target_id}[/cyan] "
                        f"({len(resumed_state.messages)} 条消息, "
                        f"{resumed_state.turn_count} 轮对话)[/dim]"
                    )
                    continue

                if cmd in ("compact", "compress"):
                    if agent_state:
                        agent_state = await engine._compact(agent_state)
                        console.print("[dim]Context compressed.[/dim]")
                    else:
                        console.print("  [dim]No active session.[/dim]")
                    continue

                if cmd_name == "yolo":
                    if agent_state:
                        agent_state.permission_state.yolo_mode = (
                            not agent_state.permission_state.yolo_mode
                        )
                        status = "ON" if agent_state.permission_state.yolo_mode else "OFF"
                        color = (
                            "bold yellow"
                            if agent_state.permission_state.yolo_mode
                            else "dim"
                        )
                        console.print(f"  YOLO mode: [{color}]{status}[/{color}]")
                    else:
                        console.print(
                            "  [dim]No active session - will apply to next prompt.[/dim]"
                        )
                    continue

                if cmd == "help":
                    _cmd_help(console, skill_registry=skill_registry, cwd=current_cwd)
                    continue

                skill = (
                    skill_registry.find_visible(cmd_name, current_cwd)
                    if skill_registry is not None
                    else None
                )
                if skill is None or not skill.frontmatter.user_invocable:
                    console.print(
                        f"  [red]Unknown command: {user_input}[/red]  [dim](try /help)[/dim]"
                    )
                    continue

            console.print()
            try:
                state_to_pass = agent_state if agent_state is not None else engine._last_state
                await ui_runtime.run_submit_message(
                    user_input=user_input,
                    state_to_pass=state_to_pass,
                    session_id=session_id,
                )

                agent_state = engine._last_state

                if agent_state:
                    _save_session_snapshot(agent_state)

            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.exception("Error in engine loop")
                console.print()
                console.print(
                    Panel(str(exc)[:500], title="Error", border_style="red"),
                )
                engine.reset()

            console.print()
    finally:
        await ui_runtime.shutdown()
        await engine.shutdown()
