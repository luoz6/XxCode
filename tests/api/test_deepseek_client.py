"""Tests for DeepSeek API adapter behavior."""

import pytest

from xxcode.api.client import DeepSeekClient


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
