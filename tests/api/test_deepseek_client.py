"""Tests for DeepSeek API adapter behavior."""

import pytest

from xxcode.api.client import DeepSeekClient


def _make_client() -> DeepSeekClient:
    return DeepSeekClient(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://api.deepseek.com",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://api.deepseek.com/",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://api.deepseek.com/v1",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://api.deepseek.com/v1/",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://api.deepseek.com/chat/completions",
            "https://api.deepseek.com/chat/completions",
        ),
    ],
)
def test_deepseek_chat_completions_url_normalizes_base_url(base_url, expected):
    client = DeepSeekClient(
        api_key="test-key",
        base_url=base_url,
        model="deepseek-chat",
    )

    assert client._chat_completions_url() == expected


def test_build_messages_tool_call_only_assistant_has_explicit_empty_content():
    client = _make_client()
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "glob_match",
                    "input": {"pattern": "**/*.py"},
                }
            ],
        }
    ]

    built = client._build_messages("", messages)

    assert built[0] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "glob_match",
                    "arguments": '{"pattern": "**/*.py"}',
                },
            }
        ],
    }
    assert built[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Tool execution was interrupted or lost before the provider request was retried.",
    }


def test_build_messages_emits_tool_results_adjacent_to_assistant_tool_calls():
    client = _make_client()
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Find Python files"}],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "glob_match",
                    "input": {"pattern": "**/*.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "tests/test_skill_integration.py",
                },
                {
                    "type": "text",
                    "text": "<system_hint>\nOne or more tool executions failed.\n</system_hint>",
                },
            ],
        },
    ]

    built = client._build_messages("", messages)

    assert built[0] == {"role": "user", "content": "Find Python files"}
    assert built[1]["role"] == "assistant"
    assert built[1]["content"] == ""
    assert built[1]["tool_calls"][0]["id"] == "call-1"
    assert built[2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "tests/test_skill_integration.py",
    }
    assert built[3] == {
        "role": "user",
        "content": "<system_hint>\nOne or more tool executions failed.\n</system_hint>",
    }


def test_build_messages_keeps_multi_tool_turn_atomic():
    client = _make_client()
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "glob_match",
                    "input": {"pattern": "**/*.py"},
                },
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "grep_search",
                    "input": {"pattern": "TODO", "path": "src"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "a.py\nb.py",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "src/x.py:3: TODO",
                },
            ],
        },
    ]

    built = client._build_messages("", messages)

    assert built[0]["role"] == "assistant"
    assert [call["id"] for call in built[0]["tool_calls"]] == ["call-1", "call-2"]
    assert built[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "a.py\nb.py",
    }
    assert built[2] == {
        "role": "tool",
        "tool_call_id": "call-2",
        "content": "src/x.py:3: TODO",
    }


def test_build_messages_rejects_and_drops_mixed_id_tool_result_carrier():
    client = _make_client()
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "glob_match",
                    "input": {"pattern": "**/*.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "a.py",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "other turn result",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "grep_search",
                    "input": {"pattern": "TODO", "path": "src"},
                }
            ],
        },
    ]

    built = client._build_messages("", messages)

    assert built[0]["role"] == "assistant"
    assert built[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Tool execution was interrupted or lost before the provider request was retried.",
    }
    assert built[2]["role"] == "assistant"
    assert built[3] == {
        "role": "tool",
        "tool_call_id": "call-2",
        "content": "Tool execution was interrupted or lost before the provider request was retried.",
    }
    assert not any(
        message.get("role") == "tool" and message.get("content") == "a.py"
        for message in built
    )
    assert not any(
        message.get("role") == "tool" and message.get("content") == "other turn result"
        for message in built
    )


def test_build_messages_synthesizes_missing_tool_message_when_none_is_available():
    client = _make_client()
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "glob_match",
                    "input": {"pattern": "**/*.py"},
                }
            ],
        }
    ]

    built = client._build_messages("", messages)

    assert built[0]["role"] == "assistant"
    assert built[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "Tool execution was interrupted or lost before the provider request was retried.",
    }
