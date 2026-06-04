"""EditFileInput — Pydantic schema, error codes, and read-file state for the edit_file tool."""

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field


class EditErrorCode(Enum):
    """Structured error codes for edit_file failures.

    Mirrors Claude Code's error code system (§10.2.4).  Each code
    tells the model exactly what went wrong so it can self-correct
    without guessing.  The plain-text error message is supplementary
    context; the code is the machine-actionable signal.
    """

    OK = 0
    NO_OP = 1              # old_string == new_string
    PERMISSION_DENIED = 2  # Path matches user deny rules
    EMPTY_OLD_ON_EXISTING = 3  # old_string empty but file has content
    FILE_NOT_FOUND = 4     # Target file doesn't exist
    NOTEBOOK_REDIRECT = 5  # .ipynb — use NotebookEditTool instead
    UNREAD_FILE = 6        # File never read (read-before-edit enforced)
    STALE_READ = 7         # mtime changed since last read
    STRING_NOT_FOUND = 8   # old_string not present in file
    MULTIPLE_MATCHES = 9   # >1 matches and replace_all=false
    FILE_TOO_LARGE = 10    # File exceeds size limit
    WRITE_FAILED = 11      # Disk write error
    READ_FAILED = 12       # Disk read error
    CASCADING_EDIT = 13    # old_string is substring of previous edit's new_string


def _format_error(code: EditErrorCode, detail: str = "") -> str:
    """Format a structured error message with error code prefix.

    The [ErrCode N] prefix lets models parse failures programmatically
    while the detail string provides human-readable context.
    """
    if detail:
        return f"[ErrCode {code.value}] {code.name}: {detail}"
    return f"[ErrCode {code.value}] {code.name}"


class EditFileInput(BaseModel):
    """Input schema for edit_file — exact string replacement in an existing file.

    By default, old_string must appear exactly once in the file (uniqueness
    constraint). Use replace_all=True to substitute every occurrence.
    """

    file_path: str = Field(description="The absolute path to the file to edit")
    old_string: str = Field(description="The exact text to replace")
    new_string: str = Field(
        description="The text to replace it with (must differ from old_string)"
    )
    replace_all: bool = Field(
        default=False,
        description="If true, replace all occurrences. Otherwise old_string must appear exactly once.",
    )


# ═════════════════════════════════════════════════════════════════════
# Read-file state — enables read-before-edit enforcement and
# external-modification detection (P1: Claude Code §10.4)
# ═════════════════════════════════════════════════════════════════════

@dataclass
class FileStateEntry:
    """Per-file read state cached after each read_file call.

    Stored in AgentState.read_file_state, keyed by absolute file path.
    """
    content: str            # Full file content at time of read
    timestamp: float        # mtime (os.stat().st_mtime) at time of read
    is_partial_view: bool = False   # True if offset/limit was used
    line_endings: str = "\n"        # Detected line ending: "\n" or "\r\n"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "timestamp": self.timestamp,
            "is_partial_view": self.is_partial_view,
            "line_endings": self.line_endings,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileStateEntry":
        return cls(
            content=data.get("content", ""),
            timestamp=data.get("timestamp", 0.0),
            is_partial_view=data.get("is_partial_view", False),
            line_endings=data.get("line_endings", "\n"),
        )


def detect_line_endings(content: str) -> str:
    """Detect line ending style from file content.

    Returns "\\r\\n" if CRLF is the dominant line ending, "\\n" otherwise.
    Scans the first 4096 characters for efficiency.
    """
    sample = content[:4096]
    crlf_count = sample.count("\r\n")
    lf_count = sample.count("\n") - crlf_count  # LF without preceding CR
    if crlf_count > lf_count:
        return "\r\n"
    return "\n"
