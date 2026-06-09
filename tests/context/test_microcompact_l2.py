from xxcode.context.micro import microcompact_messages


def _round(tool_name: str, tool_use_id: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ],
        },
    ]


def _result_contents(messages: list[dict]) -> list[str]:
    return [
        block["content"]
        for message in messages
        for block in message.get("content", [])
        if block.get("type") == "tool_result"
    ]


def test_cold_microcompact_clears_stale_results_and_emits_no_edits():
    messages = []
    messages.extend(_round("read_file", "read-1", "old read"))
    messages.extend(_round("run_shell", "shell-1", "old shell"))
    messages.extend(_round("grep_search", "grep-1", "recent grep"))

    compressed, edits = microcompact_messages(messages, is_cache_cold=True, keep_recent=1)

    assert edits == []
    assert _result_contents(compressed) == [
        "[Old tool result content cleared]",
        "[Old tool result content cleared]",
        "recent grep",
    ]
    assert _result_contents(messages) == ["old read", "old shell", "recent grep"]


def test_warm_microcompact_preserves_content_and_emits_cache_edits():
    messages = []
    messages.extend(_round("read_file", "read-1", "old read"))
    messages.extend(_round("run_shell", "shell-1", "recent shell"))

    compressed, edits = microcompact_messages(messages, is_cache_cold=False, keep_recent=1)

    assert _result_contents(compressed) == ["old read", "recent shell"]
    assert [(edit.tool_use_id, edit.action) for edit in edits] == [("read-1", "delete")]


def test_keep_recent_zero_is_clamped_to_one():
    messages = []
    messages.extend(_round("read_file", "read-1", "old read"))
    messages.extend(_round("run_shell", "shell-1", "recent shell"))

    compressed, edits = microcompact_messages(messages, is_cache_cold=True, keep_recent=0)

    assert edits == []
    assert _result_contents(compressed) == [
        "[Old tool result content cleared]",
        "recent shell",
    ]


def test_xxcode_edit_and_write_tools_are_compressible():
    messages = []
    messages.extend(_round("edit_file", "edit-1", "old edit"))
    messages.extend(_round("write_file", "write-1", "recent write"))

    compressed, edits = microcompact_messages(messages, is_cache_cold=False, keep_recent=1)

    assert _result_contents(compressed) == ["old edit", "recent write"]
    assert [edit.tool_use_id for edit in edits] == ["edit-1"]


def test_claude_display_aliases_are_not_xxcode_builtins():
    messages = []
    messages.extend(_round("FileRead", "read-1", "old read"))
    messages.extend(_round("Bash", "shell-1", "old shell"))

    compressed, edits = microcompact_messages(messages, is_cache_cold=True, keep_recent=1)

    assert edits == []
    assert _result_contents(compressed) == ["old read", "old shell"]
