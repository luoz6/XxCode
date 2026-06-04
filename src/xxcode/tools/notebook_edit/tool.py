"""NotebookEditTool — cell-level editing for Jupyter notebooks (.ipynb).

Redirected from EditFileTool when the target is a .ipynb file.
Uses the standard library json module (no nbformat dependency).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import Tool
from ..path_utils import check_allowed_write_roots, resolve_tool_path
from .types import NotebookEditInput


class NotebookEditTool(Tool):
    """Edit a Jupyter notebook (.ipynb) cell by replacing its source.

    Notebooks are JSON documents. This tool reads the notebook, locates
    the target cell by its id, replaces/inserts/deletes the source, and
    writes the updated notebook back. JSON formatting (indentation) is
    preserved from the original file.
    """

    name = "notebook_edit"
    description = (
        "Completely replaces the contents of a specific cell in a Jupyter "
        "notebook (.ipynb file) with new source. cell_id identifies the "
        "cell to edit. Use edit_mode=insert to add a new cell after the "
        "identified cell, or edit_mode=delete to remove the cell."
    )
    input_schema = NotebookEditInput

    _is_read_only = False
    _is_destructive = False

    def confirms_file_paths(self) -> bool:
        return True

    # ── Rendering ────────────────────────────────────────────────

    def render_tool_use(self, input: BaseModel) -> str:
        """Single-line: 'Edit notebook cell[N] in file.ipynb'."""
        assert isinstance(input, NotebookEditInput)
        name = Path(input.notebook_path).name
        mode = f" ({input.edit_mode})" if input.edit_mode != "replace" else ""
        return f"Edit notebook{mode}: {name} cell[{input.cell_id[:8]}]"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """Compact summary."""
        if is_error:
            return f"Notebook edit failed: {content[:150]}"
        return "Notebook edit OK"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """Batch summary."""
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        names = [Path(inp.notebook_path).name for inp in inputs]  # type: ignore[union-attr]
        return f"Edit notebook: {len(inputs)} edits in {', '.join(set(names))}"

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """Resolve relative paths to absolute using cwd context."""
        assert isinstance(input, NotebookEditInput)
        fp = Path(input.notebook_path)
        if not fp.is_absolute():
            cwd = context.get("cwd", str(Path.cwd()))
            fp = Path(cwd) / fp
        return NotebookEditInput(
            notebook_path=str(fp.resolve()),
            cell_id=input.cell_id,
            new_source=input.new_source,
            cell_type=input.cell_type,
            edit_mode=input.edit_mode,
        )

    # ── Validation ───────────────────────────────────────────────

    async def validate_input(
        self, input: NotebookEditInput, context: dict[str, Any],
    ) -> tuple[bool, str]:
        """Verify notebook exists and is a valid .ipynb file."""
        path = resolve_tool_path(input.notebook_path, context)
        ok, msg = check_allowed_write_roots(
            path,
            context.get("allowed_write_roots"),
        )
        if not ok:
            return False, msg

        if not path.exists():
            hint = ""
            if not path.is_absolute():
                hint = " (path is relative — use absolute paths)"
            return False, f"File not found: {input.notebook_path}{hint}"

        if not path.is_file():
            return False, f"Not a file: {input.notebook_path}"

        if path.suffix.lower() != ".ipynb":
            return False, (
                f"Not a notebook file: {input.notebook_path}. "
                f"notebook_edit only works with .ipynb files."
            )

        if input.edit_mode == "insert" and input.cell_type is None:
            return False, (
                "cell_type is required when edit_mode=insert. "
                "Specify 'code' or 'markdown'."
            )

        return True, ""

    # ── Core execution ───────────────────────────────────────────

    async def execute(
        self, input: NotebookEditInput, context: dict[str, Any],
    ) -> str:
        """Execute a cell-level edit on a Jupyter notebook.

        Pipeline:
          1. Read and parse notebook JSON
          2. Find target cell by id
          3. Apply edit mode (replace / insert / delete)
          4. Write updated notebook back to disk
        """
        path = resolve_tool_path(input.notebook_path, context)

        # Step 1: Read and parse notebook JSON.
        try:
            raw = path.read_text(encoding="utf-8")
            nb = json.loads(raw)
        except json.JSONDecodeError as e:
            return (
                f"Failed to parse notebook as JSON: {e}. "
                f"The file may be corrupted or not a valid .ipynb file."
            )
        except Exception as e:
            return f"Error reading notebook: {e}"

        if "cells" not in nb:
            return (
                "Notebook has no 'cells' key. "
                "The file may be corrupted or not a valid .ipynb file."
            )

        cells: list[dict] = nb["cells"]

        # Step 2: Find target cell.
        target_idx = -1
        for i, cell in enumerate(cells):
            if cell.get("id") == input.cell_id:
                target_idx = i
                break

        if input.edit_mode == "replace":
            if target_idx == -1:
                return (
                    f"Cell with id '{input.cell_id}' not found in notebook. "
                    f"The notebook has {len(cells)} cells."
                )
            old_source = cells[target_idx].get("source", "")
            cell_type = input.cell_type or cells[target_idx].get("cell_type", "code")
            cells[target_idx]["source"] = input.new_source
            cells[target_idx]["cell_type"] = cell_type
            nb["cells"] = cells

        elif input.edit_mode == "insert":
            new_cell: dict[str, Any] = {
                "cell_type": input.cell_type or "code",
                "metadata": {},
                "source": input.new_source,
            }
            if target_idx >= 0:
                cells.insert(target_idx + 1, new_cell)
            else:
                cells.insert(0, new_cell)
            nb["cells"] = cells

        elif input.edit_mode == "delete":
            if target_idx == -1:
                return (
                    f"Cell with id '{input.cell_id}' not found in notebook. "
                    f"Cannot delete a non-existent cell."
                )
            del cells[target_idx]
            nb["cells"] = cells

        # Step 3: Write updated notebook.
        try:
            indent = _detect_json_indent(raw)
            new_raw = json.dumps(nb, indent=indent, ensure_ascii=False)
            if not new_raw.endswith("\n"):
                new_raw += "\n"
            path.write_text(new_raw, encoding="utf-8")
            return (
                f"Notebook edit applied: {input.edit_mode} cell "
                f"in {input.notebook_path}"
            )
        except Exception as e:
            return f"Error writing notebook: {e}"


# ── Helpers ───────────────────────────────────────────────────────


def _detect_json_indent(raw: str) -> int:
    """Detect the indentation level used in a JSON string.

    Examines the first few lines of the JSON to determine whether
    the file uses 1, 2, 4, or tab indentation.
    """
    lines = raw.split("\n")
    for line in lines[1:10]:
        stripped = line.lstrip()
        if stripped and stripped[0] != "{":
            indent_len = len(line) - len(line.lstrip())
            if indent_len > 0:
                return indent_len
    return 1  # Default to single-space indent (nbformat default)
