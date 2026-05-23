"""Tool result truncation — preserves head and tail for context."""

_MAX_CHARS = 50_000


def truncate_result(content: str, max_chars: int = _MAX_CHARS) -> str:
    """Truncate a string to max_chars, keeping head and tail 50% each.

    This ensures both the beginning context and ending results are preserved
    when tool output is too large to send to the model.
    """
    if len(content) <= max_chars:
        return content

    half = max_chars // 2
    head = content[:half]
    tail = content[-half:]

    removed = len(content) - max_chars
    return f"{head}\n\n... [{removed} characters truncated] ...\n\n{tail}"
