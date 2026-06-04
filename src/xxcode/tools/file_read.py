"""read_file tool — reads a file with line numbers (cat -n style).

Updates AgentState.read_file_state after each read for P1
read-before-edit enforcement and external-modification detection.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool
from .file_edit.types import FileStateEntry, detect_line_endings
from .path_utils import check_allowed_read_roots, resolve_tool_path


class ReadFileInput(BaseModel):
    file_path: str = Field(description="The absolute path to the file to read")
    offset: int = Field(default=0, description="Line number to start reading from (0-indexed)")
    limit: int | None = Field(default=None, description="Maximum number of lines to read")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file from the local filesystem with line numbers. Supports offset and limit for large files."
    input_schema = ReadFileInput

    _is_read_only = True
    _is_concurrency_safe = True
    _max_output_chars = 200_000  # Large files read in full need more room

    async def validate_input(self, input: ReadFileInput, context: dict[str, Any]) -> tuple[bool, str]:
        """Stage 2: verify the file exists and is a regular file."""
        path = resolve_tool_path(input.file_path, context)
        allowed_roots = context.get("allowed_read_roots")
        ok, msg = check_allowed_read_roots(path, allowed_roots)
        if not ok:
            return False, msg
        if not path.exists():
            return False, f"File not found: {input.file_path}"
        if not path.is_file():
            return False, f"Not a file: {input.file_path}"
        return True, ""

    async def execute(self, input: ReadFileInput, context: dict[str, Any]) -> str:
        path = resolve_tool_path(input.file_path, context)
        try:
            raw_bytes = path.read_bytes()
            # Detect encoding (simple BOM check + UTF-8 fallback)
            if raw_bytes[:2] == b'\xff\xfe':
                content = raw_bytes.decode("utf-16-le", errors="replace")
            else:
                content = raw_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        # Detect line endings BEFORE normalization (needed for FileStateEntry)
        line_endings = detect_line_endings(content)
        # Normalize CRLF → LF so line splitting produces clean results
        if line_endings == "\r\n":
            content = content.replace("\r\n", "\n")

        # Update readFileState for P1 read-before-edit enforcement
        self._update_read_state(
            context, str(path), content, line_endings,
            is_partial=(input.offset > 0 or input.limit is not None),
        )

        lines = content.split("\n")
        start = max(0, input.offset)
        end = min(len(lines), start + input.limit) if input.limit else len(lines)

        result_lines = []
        for i in range(start, end):
            result_lines.append(f"{i + 1}\t{lines[i]}")

        return "\n".join(result_lines)

    def _update_read_state(
        self, context: dict[str, Any], file_path: str,
        content: str, line_endings: str, is_partial: bool,
    ) -> None:
        """Record the read in AgentState.read_file_state."""
        state = context.get("parent_state")
        if state is None or not hasattr(state, "read_file_state"):
            return
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            mtime = 0.0
        state.read_file_state[file_path] = FileStateEntry(
            content=content,
            timestamp=mtime,
            is_partial_view=is_partial,
            line_endings=line_endings,
        )

    def render_tool_use(self, input: BaseModel) -> str:
        """Single-line: '📖 Read src/main.py' with optional offset/limit."""
        assert isinstance(input, ReadFileInput)
        name = Path(input.file_path).name
        if input.offset or input.limit:
            return f"📖 Read {name} (L{input.offset}-{input.offset + (input.limit or 0)})"
        return f"📖 Read {name}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """Compact summary: line count or error snippet."""
        if is_error:
            return f"Read failed: {content[:150]}"
        line_count = content.count("\n") + 1 if content else 0
        return f"Read {line_count} lines"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch summary: '📖 Read 3 files: a.py, b.py, c.py'"""
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        names = [Path(inp.file_path).name for inp in inputs]  # type: ignore[union-attr]
        return f"📖 Read {len(inputs)} files: {', '.join(names[:5])}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve relative paths to absolute using cwd context."""
        assert isinstance(input, ReadFileInput)
        fp = Path(input.file_path)
        if not fp.is_absolute():
            cwd = context.get("cwd", str(Path.cwd()))
            fp = Path(cwd) / fp
        return ReadFileInput(
            file_path=str(fp.resolve()),
            offset=input.offset,
            limit=input.limit,
        )
