"""read_file tool — reads a file with line numbers (cat -n style)."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool


class ReadFileInput(BaseModel):
    file_path: str = Field(description="The absolute path to the file to read")
    offset: int = Field(default=0, description="Line number to start reading from (0-indexed)")
    limit: int | None = Field(default=None, description="Maximum number of lines to read")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file from the local filesystem with line numbers. Supports offset and limit for large files."
    input_schema = ReadFileInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def execute(self, input: ReadFileInput, context: dict[str, Any]) -> str:
        path = Path(input.file_path)
        if not path.exists():
            return f"Error: File not found: {input.file_path}"

        if not path.is_file():
            return f"Error: Not a file: {input.file_path}"

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.split("\n")
        start = max(0, input.offset)
        end = min(len(lines), start + input.limit) if input.limit else len(lines)

        result_lines = []
        for i in range(start, end):
            result_lines.append(f"{i + 1}\t{lines[i]}")

        return "\n".join(result_lines)
