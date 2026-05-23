"""System prompt builder — loads template and substitutes runtime variables."""

from datetime import date
from pathlib import Path

from .context import get_git_context, load_claude_md


_TEMPLATE_PATH = Path(__file__).parent.parent / "assets" / "system-prompt.md"


def build_system_prompt(cwd: Path | None = None) -> str:
    """Build the full system prompt by loading the template and substituting variables.

    Args:
        cwd: Working directory for context collection. Defaults to current directory.

    Returns:
        The fully rendered system prompt string.
    """
    if cwd is None:
        cwd = Path.cwd()

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    git_context = get_git_context(cwd)
    claude_md = load_claude_md(cwd)

    import os
    import platform

    return (
        template
        .replace("{{cwd}}", str(cwd))
        .replace("{{date}}", date.today().isoformat())
        .replace("{{platform}}", platform.platform())
        .replace("{{shell}}", os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown")))
        .replace("{{git_context}}", f"\n## Git Context\n\n{git_context}" if git_context else "")
        .replace("{{claude_md}}", f"\n## Project Instructions (CLAUDE.md)\n\n{claude_md}" if claude_md else "")
    )
