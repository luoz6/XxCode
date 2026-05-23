"""edit_file tool — search-and-replace with uniqueness constraint."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import Tool


class EditFileInput(BaseModel):
    file_path: str = Field(description="The absolute path to the file to edit")
    old_string: str = Field(description="The exact text to replace")
    new_string: str = Field(description="The text to replace it with (must differ from old_string)")
    replace_all: bool = Field(
        default=False,
        description="If true, replace all occurrences. Otherwise the old_string must appear exactly once.",
    )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Perform exact string replacement in an existing file. "
        "By default, old_string must appear exactly once in the file. "
        "Use replace_all=true to substitute every occurrence."
    )
    input_schema = EditFileInput

    async def execute(self, input: EditFileInput, context: dict[str, Any]) -> str:
        if input.old_string == input.new_string:
            return "Error: old_string and new_string must be different."

        path = Path(input.file_path)

        if not path.exists():
            return f"Error: File not found: {input.file_path}"

        if not path.is_file():
            return f"Error: Not a file: {input.file_path}"

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error reading file: {e}"

        count = content.count(input.old_string)

        if count == 0:
            return "Error: old_string not found in file. The file may have changed since you last read it."

        if not input.replace_all and count > 1:
            return (
                f"Error: old_string appears {count} times in the file. "
                f"Include more surrounding context to make it unique, or use replace_all=true."
            )

        new_content = content.replace(input.old_string, input.new_string)

        try:
            path.write_text(new_content, encoding="utf-8")
            replaced = count if input.replace_all else 1
            return f"Edit applied successfully: {replaced} occurrence(s) replaced in {input.file_path}"
        except Exception as e:
            return f"Error writing file: {e}"
