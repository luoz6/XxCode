"""CLI entry point — dispatches to REPL mode or single-shot mode."""

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

from .agent import QueryEngine, create_query_engine
from .cli import create_ui
from .config import Config, get_config, set_config
from .memory import (
    ensure_memory_directory,
    is_auto_memory_enabled,
    resolve_memory_directory,
    run_cleanup,
    write_memory_index,
)
from .ui.session import SessionStore

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xxcode",
        description="XxCode — an AI-powered coding assistant",
    )

    parser.add_argument(
        "-p", "--prompt",
        type=str,
        default=None,
        help="Single-shot mode: execute one prompt and exit.",
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model to use (default: claude-sonnet-4-6)",
    )

    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key (default: $XXCODE_API_KEY, fallback: $ANTHROPIC_API_KEY)",
    )

    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help=(
            "API base URL (default: $XXCODE_API_BASE_URL, "
            "fallback: $ANTHROPIC_BASE_URL)"
        ),
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16000,
        help="Max output tokens per turn (default: 16000)",
    )

    parser.add_argument(
        "--yolo",
        action="store_true",
        default=False,
        help="Skip all permission prompts (dangerous, use with caution).",
    )

    parser.add_argument(
        "--cwd",
        type=str,
        default=None,
        help="Working directory (default: current directory).",
    )

    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume a previous session by ID. Use --list to see saved sessions.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="List saved sessions and exit.",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable debug logging.",
    )

    parser.add_argument(
        "--bare",
        action="store_true",
        default=False,
        help="Bare mode: disable auto-memory and other persistent features.",
    )

    parser.add_argument(
        "--ui-backend",
        type=str,
        choices=("legacy_terminal", "prompt_toolkit_fullscreen"),
        default=None,
        help="Terminal UI backend (default: legacy_terminal).",
    )

    return parser


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_single_shot(engine: QueryEngine, ui, prompt: str) -> None:
    """Run a single prompt and exit."""

    async def _run() -> None:
        from .ui.runtime import UiRuntime

        ui_runtime = UiRuntime(engine=engine, ui=ui)
        try:
            await ui_runtime.run_submit_message(
                user_input=prompt,
                state_to_pass=getattr(engine, "_last_state", None),
                session_id=uuid.uuid4().hex[:12],
            )
        finally:
            await ui_runtime.shutdown()
            await engine.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nAborted.")


def run_repl(engine: QueryEngine, ui, config: Config, initial_state=None, session_id=None, skill_registry=None) -> None:
    """Run the interactive REPL."""
    from .ui.repl import run_repl as _run_repl

    try:
        asyncio.run(_run_repl(engine, ui, config, initial_state=initial_state, session_id=session_id, skill_registry=skill_registry))
    except KeyboardInterrupt:
        print("\nGoodbye.")
    except EOFError:
        print("\nGoodbye.")


def _bootstrap_memory(config: Config, bare_mode: bool) -> Path | None:
    """Initialize the auto-memory system at startup.

    Returns the resolved memory directory path, or None if memory is disabled.
    """
    if not is_auto_memory_enabled(
        config_auto_memory_enabled=config.auto_memory_enabled,
        bare_mode=bare_mode,
    ):
        return None

    mem_dir = resolve_memory_directory(
        config_cwd=config.cwd,
        auto_memory_directory=config.auto_memory_directory,
    )
    if mem_dir is None:
        logger.info("Auto-memory disabled: not in a git repository.")
        return None

    ensure_memory_directory(mem_dir)
    run_cleanup(mem_dir)
    write_memory_index(mem_dir)
    return mem_dir


def main() -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Build config
    config = get_config()

    if args.model:
        config.api_model = args.model
    if args.api_key:
        config.api_key = args.api_key
    if args.base_url:
        config.api_base_url = args.base_url
    if args.max_tokens:
        config.api_max_tokens = args.max_tokens
    if args.yolo:
        config.yolo = True
    if args.cwd:
        config.cwd = Path(args.cwd).resolve()
    if args.ui_backend:
        config.ui_backend = args.ui_backend

    set_config(config)

    # Bootstrap auto-memory
    memory_dir = _bootstrap_memory(config, args.bare)
    if memory_dir is not None:
        config.auto_memory_directory = str(memory_dir)
        if args.verbose:
            print(f"[debug] Memory:  {memory_dir}")

    if args.verbose:
        print(f"[debug] API URL: {config.api_base_url}")
        print(f"[debug] Model:   {config.api_model}")
        print(f"[debug] Key:     <set>")

    # List sessions mode
    if args.list:
        store = SessionStore(config.session_dir)
        sessions = store.list_sessions()
        if not sessions:
            print("No saved sessions.")
        else:
            print("Saved sessions:")
            for s in sessions:
                import datetime
                dt = datetime.datetime.fromtimestamp(s.last_updated)
                print(f"  {s.session_id}  — {s.message_count} msgs, {s.turn_count} turns, last: {dt:%Y-%m-%d %H:%M}")
        return

    # Validate API settings
    if not config.api_key:
        print(
            "Error: No API key configured. Set XXCODE_API_KEY or "
            "ANTHROPIC_API_KEY, or use --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not config.api_base_url:
        print(
            "Error: No API base URL configured. Set XXCODE_API_BASE_URL or "
            "ANTHROPIC_BASE_URL, or use --base-url.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create engine and UI
    engine = create_query_engine(config)
    ui = create_ui(config)

    # Access skill registry from engine (it self-bootstraps)
    skill_registry = engine.skill_registry if config.skills_enabled else None
    if skill_registry is not None:
        ui.set_skill_registry(skill_registry)
    if args.verbose and skill_registry is not None:
        skill_count = len(skill_registry.list_all())
        print(f"[debug] Skills: {skill_count} loaded")

    # Resume session
    resumed_state = None
    resume_session_id: str | None = None
    if args.resume:
        store = SessionStore(config.session_dir)
        resumed_state = store.load_state(args.resume)
        if resumed_state:
            resume_session_id = args.resume
            recovery_snapshot = store.load_skill_recovery(args.resume)
            if recovery_snapshot is not None:
                engine.core_engine.import_skill_recovery_snapshot(recovery_snapshot)
            engine.core_engine.task_runtime.import_snapshot(
                store.load_task_runtime_snapshot(args.resume)
            )
            print(
                f"Resumed session {args.resume} — "
                f"{len(resumed_state.messages)} msgs, "
                f"{resumed_state.turn_count} turns, "
                f"YOLO={'ON' if resumed_state.permission_state.yolo_mode else 'OFF'}"
            )
        elif (messages := store.load(args.resume)):
            # Legacy .jsonl file — wrap messages in a basic AgentState
            from .agent.state import AgentState
            resumed_state = AgentState(messages=messages)
            resume_session_id = args.resume
            print(f"Resumed session {args.resume} ({len(messages)} messages) — legacy format, permissions reset.")
        else:
            print(f"Session {args.resume} not found. Starting fresh.")

    # Dispatch mode
    if args.prompt:
        run_single_shot(engine, ui, args.prompt)
    else:
        run_repl(engine, ui, config, initial_state=resumed_state, session_id=resume_session_id, skill_registry=skill_registry)


if __name__ == "__main__":
    main()
