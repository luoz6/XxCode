"""glob_match tool — file pattern matching, ignoring common noise directories."""

import glob as glob_module
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool


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

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def execute(self, input: GlobMatchInput, context: dict[str, Any]) -> str:
        search_dir = Path(input.path)
        if not search_dir.exists():
            return f"Error: Directory not found: {input.path}"

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
