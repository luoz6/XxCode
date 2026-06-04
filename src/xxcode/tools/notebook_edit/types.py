"""NotebookEditInput — Pydantic schema for the notebook_edit tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NotebookEditInput(BaseModel):
    """Input schema for notebook_edit — cell-level editing for .ipynb files.

    Replaces the source of a Jupyter notebook cell identified by its id.
    Supports insert/delete modes for adding/removing cells.
    """

    notebook_path: str = Field(
        description="The absolute path to the .ipynb notebook file to edit"
    )
    cell_id: str = Field(
        description="The ID of the cell to edit. When inserting a new cell, "
                    "the new cell will be inserted after the cell with this ID, "
                    "or at the beginning if not specified."
    )
    new_source: str = Field(
        description="The new source for the cell"
    )
    cell_type: Literal["code", "markdown"] | None = Field(
        default=None,
        description="The type of the cell (code or markdown). If not specified, "
                    "it defaults to the current cell type. If using edit_mode=insert, "
                    "this is required."
    )
    edit_mode: Literal["replace", "insert", "delete"] = Field(
        default="replace",
        description="The type of edit to make (replace, insert, delete). "
                    "Defaults to replace."
    )
