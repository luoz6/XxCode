"""CLI entry point — dispatches to REPL mode or single-shot mode."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .agent import CodingAgent, create_agent
from .config import Config, get_config, set_config
from .ui.renderer import Renderer, get_renderer
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
        help="API key (default: $ANTHROPIC_API_KEY)",
    )

    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="API base URL (default: https://api.anthropic.com)",
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

    return parser


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_single_shot(agent: CodingAgent, renderer: Renderer, prompt: str) -> None:
    """Run a single prompt and exit."""

    async def _run() -> None:
        async for event in agent.chat(prompt):
            renderer.render_event(event)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nAborted.")


def run_repl(agent: CodingAgent, renderer: Renderer, config: Config) -> None:
    """Run the interactive REPL."""
    from .ui.repl import run_repl as _run_repl

    try:
        asyncio.run(_run_repl(agent, renderer, config))
    except KeyboardInterrupt:
        print("\nGoodbye.")
    except EOFError:
        print("\nGoodbye.")


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

    set_config(config)

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

    # Validate API key
    if not config.api_key:
        print("Error: No API key set. Set ANTHROPIC_API_KEY environment variable or use --api-key.")
        sys.exit(1)

    # Create agent
    agent = create_agent(config)
    renderer = get_renderer()

    # Resume session
    if args.resume:
        store = SessionStore(config.session_dir)
        messages = store.load(args.resume)
        if messages:
            print(f"Resumed session {args.resume} ({len(messages)} messages)")
        else:
            print(f"Session {args.resume} not found. Starting fresh.")

    # Dispatch mode
    if args.prompt:
        run_single_shot(agent, renderer, args.prompt)
    else:
        run_repl(agent, renderer, config)


if __name__ == "__main__":
    main()
