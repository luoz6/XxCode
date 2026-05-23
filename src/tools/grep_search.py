"""grep_search tool — ripgrep-based content search with line limits."""

import subprocess
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool


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

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def execute(self, input: GrepSearchInput, context: dict[str, Any]) -> str:
        if not shutil.which("rg"):
            return "Error: ripgrep (rg) is not installed. Please install it from https://github.com/BurntSushi/ripgrep"

        search_path = input.path
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
