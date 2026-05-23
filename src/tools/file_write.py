"""write_file tool — creates or overwrites a file, auto-creating parent directories."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool


class WriteFileInput(BaseModel):
    file_path: str = Field(description="The absolute path to the file to write")
    content: str = Field(description="The content to write to the file")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file. Parent directories are created automatically if they do not exist."
    input_schema = WriteFileInput

    async def execute(self, input: WriteFileInput, context: dict[str, Any]) -> str:
        path = Path(input.file_path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(input.content, encoding="utf-8")
            return f"File written successfully: {input.file_path} ({len(input.content)} characters)"
        except Exception as e:
            return f"Error writing file: {e}"
