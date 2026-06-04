"""Unit tests for the 7-step message normalization pipeline."""

import sys

sys.path.insert(0, "src")

import pytest
from xxcode.agent.normalize import (
    build_error_block_map,
    filter_virtual_messages,
    fix_tool_pairings,
    handle_thinking_blocks,
    merge_split_messages,
    normalize_messages,
    reorder_attachments,
    strip_internal_elements,
)


# ── Test fixtures ───────────────────────────────────────────────────

def make_msg(role, content, **kwargs):
    """Helper to build a message dict."""
    msg = {"role": role, "content": content}
    msg.update(kwargs)
    return msg


# ── Step 1: reorder_attachments ─────────────────────────────────────

class TestReorderAttachments:
    def test_no_attachments_unmodified(self):
        msgs = [
            make_msg("user", [{"type": "text", "text": "hello"}]),
            make_msg("assistant", [{"type": "text", "text": "hi"}]),
        ]
        result = reorder_attachments(msgs)
        assert result == msgs

    def test_attachment_bubbles_to_front(self):
        msgs = [
            make_msg("user", [
                {"type": "text", "text": "see this:"},
                {"type": "image", "url": "photo.jpg"},
            ]),
        ]
        result = reorder_attachments(msgs)
        types = [b["type"] for b in result[0]["content"]]
        assert types == ["image", "text"]

    def test_document_bubbles_to_front(self):
        msgs = [
            make_msg("user", [
                {"type": "text", "text": "read this pdf"},
                {"type": "document", "url": "doc.pdf"},
            ]),
        ]
        result = reorder_attachments(msgs)
        types = [b["type"] for b in result[0]["content"]]
        assert types == ["document", "text"]

    def test_multiple_attachments_preserve_order(self):
        msgs = [
            make_msg("user", [
                {"type": "text", "text": "x"},
                {"type": "image", "url": "a.jpg"},
                {"type": "text", "text": "y"},
                {"type": "document", "url": "b.pdf"},
            ]),
        ]
        result = reorder_attachments(msgs)
        types = [b["type"] for b in result[0]["content"]]
        assert types == ["image", "document", "text", "text"]

    def test_non_list_content_passthrough(self):
        msgs = [make_msg("user", "string content")]
        result = reorder_attachments(msgs)
        assert result == msgs


# ── Step 2: filter_virtual_messages ─────────────────────────────────

class TestFilterVirtualMessages:
    def test_removes_virtual(self):
        msgs = [
            make_msg("user", [{"type": "text", "text": "real"}]),
            make_msg("user", [{"type": "text", "text": "fake"}], isVirtual=True),
        ]
        result = filter_virtual_messages(msgs)
        assert len(result) == 1
        assert result[0]["content"][0]["text"] == "real"

    def test_all_real_unmodified(self):
        msgs = [
            make_msg("user", [{"type": "text", "text": "a"}]),
            make_msg("assistant", [{"type": "text", "text": "b"}]),
        ]
        result = filter_virtual_messages(msgs)
        assert result == msgs

    def test_all_virtual_returns_empty(self):
        msgs = [make_msg("user", [], isVirtual=True)]
        result = filter_virtual_messages(msgs)
        assert result == []


# ── Step 3: build_error_block_map ───────────────────────────────────

class TestBuildErrorBlockMap:
    def test_empty_errors(self):
        assert build_error_block_map([]) == {}

    def test_pdf_too_large(self):
        assert build_error_block_map(["PDF too large"]) == {
            "PDF too large": ["document"]
        }

    def test_image_too_large(self):
        assert build_error_block_map(["image too large"]) == {
            "image too large": ["image"]
        }

    def test_unsupported_media(self):
        result = build_error_block_map(["unsupported_media error"])
        assert "unsupported_media" in result
        assert set(result["unsupported_media"]) == {"image", "document"}

    def test_unknown_error_ignored(self):
        assert build_error_block_map(["some other error"]) == {}

    def test_multiple_matches(self):
        result = build_error_block_map(["PDF too large", "image too large"])
        assert len(result) == 2
        assert result["PDF too large"] == ["document"]
        assert result["image too large"] == ["image"]


# ── Step 4: strip_internal_elements ─────────────────────────────────

class TestStripInternalElements:
    def test_removes_tool_reference(self):
        msgs = [
            make_msg("assistant", [
                {"type": "text", "text": "ok"},
                {"type": "tool_reference", "tool_id": "x"},
            ]),
        ]
        result = strip_internal_elements(msgs, {})
        types = [b["type"] for b in result[0]["content"]]
        assert "tool_reference" not in types
        assert len(types) == 1

    def test_removes_advisor_block(self):
        msgs = [
            make_msg("user", [
                {"type": "advisor_block", "text": "internal"},
                {"type": "text", "text": "visible"},
            ]),
        ]
        result = strip_internal_elements(msgs, {})
        types = [b["type"] for b in result[0]["content"]]
        assert "advisor_block" not in types

    def test_removes_error_media(self):
        error_map = {"image too large": ["image"]}
        msgs = [
            make_msg("user", [
                {"type": "image", "url": "big.jpg"},
                {"type": "text", "text": "caption"},
            ]),
        ]
        result = strip_internal_elements(msgs, error_map)
        types = [b["type"] for b in result[0]["content"]]
        assert "image" not in types
        assert types == ["text"]

    def test_removes_empty_message(self):
        error_map = {"image too large": ["image"]}
        msgs = [
            make_msg("user", [{"type": "image", "url": "x.jpg"}]),
            make_msg("user", [{"type": "text", "text": "kept"}]),
        ]
        result = strip_internal_elements(msgs, error_map)
        assert len(result) == 1
        assert result[0]["content"][0]["text"] == "kept"


# ── Step 5: handle_thinking_blocks ──────────────────────────────────

class TestHandleThinkingBlocks:
    def test_deepseek_preserves_thinking(self):
        msgs = [
            make_msg("assistant", [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]),
        ]
        result = handle_thinking_blocks(msgs, "deepseek-v4-pro")
        types = [b["type"] for b in result[0]["content"]]
        assert "thinking" in types
        assert "text" in types

    def test_unknown_model_strips_thinking(self):
        msgs = [
            make_msg("assistant", [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]),
        ]
        result = handle_thinking_blocks(msgs, "gpt-5")
        types = [b["type"] for b in result[0]["content"]]
        assert "thinking" not in types
        assert types == ["text"]

    def test_strips_thinking_from_user_messages(self):
        msgs = [
            make_msg("user", [
                {"type": "thinking", "thinking": "x"},
                {"type": "text", "text": "hello"},
            ]),
        ]
        result = handle_thinking_blocks(msgs, "claude-sonnet-4-6")
        types = [b["type"] for b in result[0]["content"]]
        assert "thinking" not in types

    def test_strips_signature_for_unsupported(self):
        msgs = [
            make_msg("assistant", [
                {"type": "thinking", "thinking": "hmm", "signature": "sig1"},
                {"type": "signature", "signature": "sig2"},
            ]),
        ]
        result = handle_thinking_blocks(msgs, "gpt-5")
        # Empty messages are dropped
        assert len(result) == 0

    def test_preserves_redacted_thinking_for_supported(self):
        msgs = [
            make_msg("assistant", [
                {"type": "redacted_thinking", "data": "..."},
                {"type": "text", "text": "ok"},
            ]),
        ]
        result = handle_thinking_blocks(msgs, "deepseek-v4-flash")
        types = [b["type"] for b in result[0]["content"]]
        assert "redacted_thinking" in types


# ── Step 6: merge_split_messages ────────────────────────────────────

class TestMergeSplitMessages:
    def test_merges_same_id(self):
        msgs = [
            make_msg("assistant", [{"type": "text", "text": "part1"}], id="msg-1"),
            make_msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}], id="msg-1"),
        ]
        result = merge_split_messages(msgs)
        assert len(result) == 1
        types = [b["type"] for b in result[0]["content"]]
        assert types == ["text", "tool_use"]

    def test_different_id_not_merged(self):
        msgs = [
            make_msg("assistant", [{"type": "text", "text": "a"}], id="msg-1"),
            make_msg("assistant", [{"type": "text", "text": "b"}], id="msg-2"),
        ]
        result = merge_split_messages(msgs)
        assert len(result) == 2

    def test_no_id_not_merged(self):
        msgs = [
            make_msg("assistant", [{"type": "text", "text": "a"}]),
            make_msg("assistant", [{"type": "text", "text": "b"}]),
        ]
        result = merge_split_messages(msgs)
        assert len(result) == 2

    def test_non_assistant_not_merged(self):
        msgs = [
            make_msg("user", [{"type": "text", "text": "a"}], id="msg-1"),
            make_msg("user", [{"type": "text", "text": "b"}], id="msg-1"),
        ]
        result = merge_split_messages(msgs)
        assert len(result) == 2

    def test_three_way_merge(self):
        msgs = [
            make_msg("assistant", [{"type": "text", "text": "a"}], id="msg-1"),
            make_msg("assistant", [{"type": "tool_use", "id": "t1", "name": "r", "input": {}}], id="msg-1"),
            make_msg("assistant", [{"type": "tool_use", "id": "t2", "name": "w", "input": {}}], id="msg-1"),
        ]
        result = merge_split_messages(msgs)
        assert len(result) == 1
        assert len(result[0]["content"]) == 3


# ── Step 7: fix_tool_pairings ───────────────────────────────────────

class TestFixToolPairings:
    def test_no_orphans_unmodified(self):
        msgs = [
            make_msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]),
            make_msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        ]
        result = fix_tool_pairings(msgs)
        assert result == msgs

    def test_orphan_tool_use_gets_synthetic_result(self):
        msgs = [
            make_msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]),
        ]
        result = fix_tool_pairings(msgs)
        assert len(result) == 2
        assert result[1]["role"] == "user"
        assert result[1]["content"][0]["tool_use_id"] == "t1"

    def test_orphan_tool_result_gets_synthetic_use(self):
        msgs = [
            make_msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        ]
        result = fix_tool_pairings(msgs)
        # Should have injected an assistant message before the user message
        assert len(result) >= 2
        # Find the synthetic tool_use
        all_uses = []
        for msg in result:
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    all_uses.append(block)
        assert any(b["id"] == "t1" for b in all_uses)

    def test_fast_path_no_messages(self):
        assert fix_tool_pairings([]) == []


# ── Integration: normalize_messages orchestrator ────────────────────

class TestNormalizeMessages:
    def test_full_pipeline(self):
        msgs = [
            # Virtual message (removed in step 2)
            make_msg("user", [{"type": "text", "text": "fake"}], isVirtual=True),
            # Normal user message
            make_msg("user", [
                {"type": "text", "text": "read this"},
                {"type": "image", "url": "photo.jpg"},
            ]),
            # Assistant with thinking (preserved for deepseek)
            make_msg("assistant", [
                {"type": "thinking", "thinking": "analyzing..."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "grep_search", "input": {}},
            ]),
            # Split continuation (same id as previous)
            make_msg("assistant", [
                {"type": "tool_use", "id": "t3", "name": "glob_match", "input": {}},
            ], id="merged"),
            make_msg("assistant", [
                {"type": "text", "text": "found 3 files"},
            ], id="merged"),
            # Tool results (t1 and t3, but missing t2 result)
            make_msg("user", [
                {"type": "tool_result", "tool_use_id": "t1", "content": "content here"},
                {"type": "tool_result", "tool_use_id": "t3", "content": "found files"},
            ]),
        ]

        result = normalize_messages(
            msgs,
            model_family="deepseek-v4-pro",
            recent_errors=[],
        )

        # Verify: virtual message removed
        assert not any(m.get("isVirtual") for m in result)

        # Verify: attachment reordered in user message
        for msg in result:
            content = msg.get("content", [])
            if isinstance(content, list) and len(content) > 1:
                for i, b in enumerate(content):
                    if b.get("type") in ("image", "document"):
                        # Should be before any text/tool blocks
                        remaining = [c["type"] for c in content[i + 1:]]
                        for rt in remaining:
                            assert rt not in ("image", "document")

        # Verify: thinking preserved for deepseek
        all_thinking = []
        for msg in result:
            for b in msg.get("content", []):
                if b.get("type") == "thinking":
                    all_thinking.append(b)
        assert len(all_thinking) > 0

        # Verify: assistant messages with same id merged
        assistant_msgs_with_id = [
            m for m in result
            if m.get("role") == "assistant" and m.get("id")
        ]
        ids = [m["id"] for m in assistant_msgs_with_id]
        assert len(ids) == len(set(ids))  # No duplicate IDs

        # Verify: orphan t2 gets synthetic tool_result
        all_results = []
        for msg in result:
            for b in msg.get("content", []):
                if b.get("type") == "tool_result":
                    all_results.append(b.get("tool_use_id"))
        assert "t2" in all_results  # Orphan was fixed

    def test_empty_input(self):
        result = normalize_messages([], "deepseek-v4-pro")
        assert result == []
