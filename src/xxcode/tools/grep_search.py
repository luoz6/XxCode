"""grep_search tool — ripgrep-based content search with line limits."""

import subprocess
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool
from .path_utils import (
    check_allowed_read_roots,
    is_broad_search_root,
    resolve_tool_path,
)


class GrepSearchInput(BaseModel):
    pattern: str = Field(description="The regular expression pattern to search for")
    path: str = Field(default=".", description="File or directory to search in")
    glob: str | None = Field(default=None, description="Glob pattern to filter files (e.g. '*.py')")
    head_limit: int = Field(default=250, description="Maximum number of matching lines to return")
    output_mode: str = Field(
        default="files_with_matches",
        description="Output mode: 'content', 'files_with_matches', or 'count'",
    )
    case_insensitive: bool = Field(default=False, description="Case insensitive search")


class GrepSearchTool(Tool):
    name = "grep_search"
    description = (
        "Search file contents using ripgrep. Returns matching lines or file paths. "
        "Supports full regex syntax, glob filtering, and output modes."
    )
    input_schema = GrepSearchInput

    _is_read_only = True
    _is_concurrency_safe = True
    _max_output_chars = 100_000  # Large searches may match thousands of lines

    # Cache the rg check across calls — spawning a subprocess every
    # time get_api_schemas() runs would be wasteful.
    _rg_available: bool | None = None

    def _check_enabled(self) -> bool:
        """Only available when ripgrep (rg) is installed."""
        if GrepSearchTool._rg_available is None:
            GrepSearchTool._rg_available = shutil.which("rg") is not None
        return GrepSearchTool._rg_available

    def render_tool_use(self, input: BaseModel) -> str:
        """Compact: '🔍 Grep 'pattern' in path'"""
        assert isinstance(input, GrepSearchInput)
        p = input.pattern[:60]
        target = Path(input.path).name if input.path != "." else "."
        flags = " -i" if input.case_insensitive else ""
        return f"Grep '{p}' in {target}{flags}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """Match count or error snippet."""
        if is_error:
            return f"Grep failed: {content[:150]}"
        if "No matches found" in content:
            return "0 matches"
        line_count = content.count("\n") + 1 if content else 0
        return f"{line_count} match(es)"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch summary."""
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        patterns = [inp.pattern[:40] for inp in inputs]  # type: ignore[union-attr]
        return f"Grep {len(inputs)} patterns: {', '.join(patterns[:5])}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve search path to absolute."""
        assert isinstance(input, GrepSearchInput)
        sp = Path(input.path)
        if not sp.is_absolute() and input.path != ".":
            cwd = context.get("cwd", str(Path.cwd()))
            sp = Path(cwd) / sp
        return GrepSearchInput(
            pattern=input.pattern,
            path=str(sp.resolve()) if input.path != "." else input.path,
            glob=input.glob,
            head_limit=input.head_limit,
            output_mode=input.output_mode,
            case_insensitive=input.case_insensitive,
        )

    async def validate_input(
        self, input: GrepSearchInput, context: dict[str, Any],
    ) -> tuple[bool, str]:
        search_path = resolve_tool_path(input.path, context)
        if is_broad_search_root(search_path):
            return False, (
                "Cannot search from a filesystem root or home directory directly. "
                "Specify a more specific path within the workspace."
            )
        ok, msg = check_allowed_read_roots(
            search_path,
            context.get("allowed_read_roots"),
        )
        if not ok:
            return False, msg
        if not search_path.exists():
            return False, f"Search path not found: {input.path}"
        return True, ""

    async def execute(self, input: GrepSearchInput, context: dict[str, Any]) -> str:
        if not shutil.which("rg"):
            return "Error: ripgrep (rg) is not installed. Please install it from https://github.com/BurntSushi/ripgrep"

        search_path = str(resolve_tool_path(input.path, context))
        cwd = str(context.get("cwd", Path.cwd()))

        args = ["rg", "--no-heading", "--with-filename", "--line-number", "--color=never"]

        if input.case_insensitive:
            args.append("-i")

        if input.glob:
            args.extend(["--glob", input.glob])

        if input.output_mode == "files_with_matches":
            args.append("-l")
        elif input.output_mode == "count":
            args.append("-c")

        args.extend([input.pattern, search_path])

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return "Error: grep search timed out (10s limit). Try narrowing the search scope."
        except Exception as e:
            return f"Error running grep: {e}"

        output = result.stdout
        if not output.strip():
            return "No matches found."

        lines = output.strip().split("\n")
        if input.head_limit and len(lines) > input.head_limit:
            truncated = lines[:input.head_limit]
            return "\n".join(truncated) + f"\n\n... ({len(lines) - input.head_limit} more results truncated)"

        return output.strip()
