"""glob_match tool — file pattern matching, ignoring common noise directories."""

import glob as glob_module
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool
from .path_utils import (
    check_allowed_read_roots,
    is_broad_search_root,
    resolve_tool_path,
)


class GlobMatchInput(BaseModel):
    pattern: str = Field(description="The glob pattern to match files against (e.g. '**/*.py')")
    path: str = Field(default=".", description="The directory to search in")


class GlobMatchTool(Tool):
    name = "glob_match"
    description = (
        "Find files matching a glob pattern. "
        "Automatically ignores .git, node_modules, __pycache__, .venv, and similar noise directories."
    )
    input_schema = GlobMatchInput

    IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"}

    _is_read_only = True
    _is_concurrency_safe = True

    def render_tool_use(self, input: BaseModel) -> str:
        """Compact: '🔎 Glob '*.py' in src/'"""
        assert isinstance(input, GlobMatchInput)
        target = Path(input.path).name if input.path != "." else "."
        return f"Glob '{input.pattern}' in {target}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """File count or error snippet."""
        if is_error:
            return f"Glob failed: {content[:150]}"
        if "No files found" in content:
            return "0 files"
        file_count = content.count("\n") + 1 if content else 0
        return f"{file_count} file(s)"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch summary."""
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        patterns = [inp.pattern for inp in inputs]  # type: ignore[union-attr]
        return f"Glob {len(inputs)} patterns: {', '.join(patterns[:5])}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve search path to absolute."""
        assert isinstance(input, GlobMatchInput)
        sp = Path(input.path)
        if not sp.is_absolute() and input.path != ".":
            cwd = context.get("cwd", str(Path.cwd()))
            sp = Path(cwd) / sp
        return GlobMatchInput(
            pattern=input.pattern,
            path=str(sp.resolve()) if input.path != "." else input.path,
        )

    async def validate_input(self, input: GlobMatchInput, context: dict[str, Any]) -> tuple[bool, str]:
        """Stage 2: verify the search directory exists."""
        search_dir = resolve_tool_path(input.path, context)
        if is_broad_search_root(search_dir):
            return False, (
                "Cannot glob from a filesystem root or home directory directly. "
                "Specify a more specific path within the workspace."
            )
        ok, msg = check_allowed_read_roots(
            search_dir,
            context.get("allowed_read_roots"),
        )
        if not ok:
            return False, msg
        if not search_dir.exists():
            return False, f"Directory not found: {input.path}"
        return True, ""

    async def execute(self, input: GlobMatchInput, context: dict[str, Any]) -> str:
        search_dir = resolve_tool_path(input.path, context)
        pattern = str(search_dir / input.pattern)

        try:
            # Use recursive glob directly for cross-platform compatibility
            matches: list[str] = []
            for p in glob_module.iglob(pattern, recursive=True):
                path_obj = Path(p)
                # Skip ignored directories
                parts = set(path_obj.parts)
                if parts & self.IGNORE_DIRS:
                    continue
                matches.append(str(path_obj))

            if not matches:
                return "No files found matching the pattern."

            # Sort by modification time (most recent first)
            try:
                matches.sort(key=lambda f: Path(f).stat().st_mtime, reverse=True)
            except OSError:
                matches.sort()

            head_limit = 100
            if len(matches) > head_limit:
                truncated = matches[:head_limit]
                return "\n".join(truncated) + f"\n\n... ({len(matches) - head_limit} more files truncated)"

            return "\n".join(matches)

        except Exception as e:
            return f"Error running glob: {e}"
