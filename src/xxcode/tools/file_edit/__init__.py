"""file_edit tool — exact string replacement with uniqueness constraint.

Directory structure:
  types.py — EditFileInput Pydantic schema
  tool.py  — EditFileTool core execution logic
  ui.py    — Human-readable rendering helpers (CLI / web / IDE)
"""

from .tool import EditFileTool
from .types import EditErrorCode, EditFileInput, FileStateEntry, detect_line_endings
from .diff import generate_diff, compute_edit_diff_stat

__all__ = [
    "EditFileTool", "EditFileInput", "EditErrorCode",
    "FileStateEntry", "detect_line_endings",
    "generate_diff", "compute_edit_diff_stat",
]
