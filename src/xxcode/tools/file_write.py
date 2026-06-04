"""write_file tool — creates or overwrites a file, auto-creating parent directories."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool
from .path_utils import check_allowed_write_roots, resolve_tool_path


class WriteFileInput(BaseModel):
    file_path: str = Field(description="The absolute path to the file to write")
    content: str = Field(description="The content to write to the file")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file. Parent directories are created automatically if they do not exist."
    input_schema = WriteFileInput

    _is_destructive = True

    def confirms_file_paths(self) -> bool:
        """On grant, the file path is added to the confirmed-paths whitelist."""
        return True

    async def validate_input(
        self, input: WriteFileInput, context: dict[str, Any],
    ) -> tuple[bool, str]:
        """Enforce allowed_write_roots when set (for restricted sub-agents)."""
        path = resolve_tool_path(input.file_path, context)
        ok, msg = check_allowed_write_roots(path, context.get("allowed_write_roots"))
        if not ok:
            return False, msg
        return True, ""

    async def execute(self, input: WriteFileInput, context: dict[str, Any]) -> str:
        path = resolve_tool_path(input.file_path, context)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(input.content, encoding="utf-8")
            return f"File written successfully: {path} ({len(input.content)} characters)"
        except Exception as e:
            return f"Error writing file: {e}"

    def render_tool_use(self, input: BaseModel) -> str:
        """Single-line with size hint."""
        assert isinstance(input, WriteFileInput)
        name = Path(input.file_path).name
        size = len(input.content)
        if size >= 1024:
            return f"Write {name} ({size // 1024}KB)"
        return f"Write {name} ({size}B)"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """Compact summary."""
        if is_error:
            return f"Write failed: {content[:150]}"
        return "Write OK"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch summary."""
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        names = [Path(inp.file_path).name for inp in inputs]  # type: ignore[union-attr]
        return f"Write {len(inputs)} files: {', '.join(names[:5])}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve relative paths to absolute."""
        assert isinstance(input, WriteFileInput)
        fp = Path(input.file_path)
        if not fp.is_absolute():
            cwd = context.get("cwd", str(Path.cwd()))
            fp = Path(cwd) / fp
        return WriteFileInput(
            file_path=str(fp.resolve()),
            content=input.content,
        )
