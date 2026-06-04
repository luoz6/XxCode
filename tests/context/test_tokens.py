"""Tests for context/tokens.py — token estimation utilities."""

import pytest
from xxcode.context.tokens import rough_estimate, token_count_with_estimation


class TestRoughEstimate:
    def test_string_estimate(self):
        # 1 token ≈ 4 characters
        assert rough_estimate("hello world") == 11 // 4  # 2

    def test_message_list_estimate(self):
        messages = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"},
        ]
        # "hello world" = 11 chars → 2, "hi there" = 8 chars → 2
        assert rough_estimate(messages) == 4

    def test_content_blocks(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "read_file", "input": {}},
                ],
            },
        ]
        result = rough_estimate(messages)
        assert result > 0

    def test_empty_list(self):
        assert rough_estimate([]) == 0

    def test_empty_string(self):
        assert rough_estimate("") == 0


class TestTokenCountWithEstimation:
    def test_empty_messages(self):
        assert token_count_with_estimation([]) == 0

    def test_no_anchor_falls_back_to_rough(self):
        messages = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = token_count_with_estimation(messages)
        # Should fall back to rough estimate since no usage anchor
        assert result == rough_estimate(messages)

    def test_with_usage_anchor(self):
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "response",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ]
        result = token_count_with_estimation(messages)
        # Anchor gives exact 15 tokens, no post-anchor messages
        assert result == 15

    def test_post_anchor_messages_estimated(self):
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "response",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            {"role": "user", "content": "another message here"},
        ]
        result = token_count_with_estimation(messages)
        # Anchor = 15, post-anchor rough = len("another message here") / 4 ≈ 4
        expected = 15 + len("another message here") // 4
        assert result == expected

    def test_split_response_anchor(self):
        """Messages from same API call (same id) should be grouped."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "part 1",
                "id": "msg_123",
            },
            {
                "role": "assistant",
                "content": "part 2",
                "id": "msg_123",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        ]
        result = token_count_with_estimation(messages)
        assert result == 30  # input 10 + output 20

    def test_finds_latest_anchor(self):
        """Should use the LAST anchor with usage, ignoring earlier ones."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "old response",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            {"role": "user", "content": "more"},
            {
                "role": "assistant",
                "content": "new response",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ]
        result = token_count_with_estimation(messages)
        # Latest anchor = 15 tokens, no post-anchor
        assert result == 15
