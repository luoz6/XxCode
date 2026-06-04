"""Tests for memory context injection formatting."""

from __future__ import annotations

import os
import time

from xxcode.memory.injection import (
    MEMORY_INDEX_SOURCE,
    MEMORY_RECALL_SOURCE,
    build_memory_index_message,
    build_recalled_memories_message,
    memory_header,
    recalled_memory_ids,
    recalled_memory_filenames,
    strip_memory_context_messages,
)
from xxcode.memory.models import MemoryType
from xxcode.memory.recall import MemoryRecall


def test_build_memory_index_message_formats_hidden_user_context(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "- [User Role](user-role.md) - Data scientist",
        encoding="utf-8",
    )

    message = build_memory_index_message(memory_dir)

    assert message is not None
    assert message["role"] == "user"
    assert message["isMeta"] is True
    assert message["metadata"]["source"] == MEMORY_INDEX_SOURCE
    text = message["content"][0]["text"]
    assert text.startswith("<system-reminder>")
    assert f"Contents of {memory_dir / 'MEMORY.md'}" in text
    assert "user's auto-memory, persists across conversations" in text
    assert "Data scientist" in text


def test_build_memory_index_message_handles_empty_index(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    message = build_memory_index_message(memory_dir)

    assert message is not None
    assert "(no indexed memories yet)" in message["content"][0]["text"]


def test_build_recalled_memories_message_uses_freshness_headers(tmp_path):
    memory_path = tmp_path / "preference.md"
    memory_path.write_text("The user prefers concise answers.", encoding="utf-8")
    now = time.time()
    os.utime(memory_path, (now, now))

    message = build_recalled_memories_message([
        MemoryRecall(
            filename="preference.md",
            file_path=memory_path,
            content=memory_path.read_text(encoding="utf-8"),
            memory_type=MemoryType.FEEDBACK,
        )
    ])

    assert message is not None
    assert message["isMeta"] is True
    assert message["metadata"]["source"] == MEMORY_RECALL_SOURCE
    text = message["content"][0]["text"]
    assert f"Memory (saved today): {memory_path}:" in text
    assert "The user prefers concise answers." in text
    assert "<memory type=" not in text


def test_memory_header_warns_for_stale_memory(tmp_path):
    memory_path = tmp_path / "old.md"
    memory_path.write_text("Old note", encoding="utf-8")
    now = time.time()
    old = now - (47 * 86_400)
    os.utime(memory_path, (old, old))

    header = memory_header(memory_path, now=now)

    assert "This memory is 47 days old." in header
    assert "Verify against current code" in header
    assert f"Memory: {memory_path}:" in header


def test_strip_memory_context_messages_can_target_one_source():
    index_msg = {
        "role": "user",
        "content": [],
        "metadata": {"xxcode_memory_context": True, "source": MEMORY_INDEX_SOURCE},
    }
    recall_msg = {
        "role": "user",
        "content": [],
        "metadata": {"xxcode_memory_context": True, "source": MEMORY_RECALL_SOURCE},
    }
    normal_msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}

    result = strip_memory_context_messages(
        [index_msg, recall_msg, normal_msg],
        source=MEMORY_INDEX_SOURCE,
    )

    assert result == [recall_msg, normal_msg]


def test_recalled_memory_filenames_reads_metadata():
    recall_msg = {
        "role": "user",
        "content": [],
        "metadata": {
            "xxcode_memory_context": True,
            "source": MEMORY_RECALL_SOURCE,
            "filenames": ["a.md", "b.md"],
        },
    }
    normal_msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}

    assert recalled_memory_filenames([recall_msg, normal_msg]) == {"a.md", "b.md"}


def test_recalled_memory_ids_prefers_explicit_ids_and_falls_back_to_filenames():
    explicit_ids_msg = {
        "role": "user",
        "content": [],
        "metadata": {
            "xxcode_memory_context": True,
            "source": MEMORY_RECALL_SOURCE,
            "filenames": ["a.md"],
            "recall_ids": ["project--a.md", "user--b.md"],
        },
    }
    fallback_msg = {
        "role": "user",
        "content": [],
        "metadata": {
            "xxcode_memory_context": True,
            "source": MEMORY_RECALL_SOURCE,
            "filenames": ["c.md"],
        },
    }

    assert recalled_memory_ids([explicit_ids_msg, fallback_msg]) == {
        "project--a.md",
        "user--b.md",
        "c.md",
    }
