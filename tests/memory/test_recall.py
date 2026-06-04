"""Tests for MEMORY.md-backed semantic memory recall."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from xxcode.memory.index import write_memory_index
from xxcode.memory.models import MemoryEntry, MemoryType
from xxcode.memory.recall import (
    MAX_RECALLED_MEMORIES,
    recall_memories_for_query,
    select_relevant_memories,
)
from xxcode.memory.store import MemoryStore


class _MockClient:
    """Fake API client that returns a controlled response for complete()."""

    def __init__(self, response_text: str = ""):
        self.response_text = response_text
        self.calls: list[dict] = []

    async def complete(
        self,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        self.calls.append({
            "system_prompt": system_prompt,
            "messages": messages or [],
            "max_tokens": max_tokens,
        })
        return self.response_text


@pytest.fixture
def mem_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _indexed_manifest(*filenames: str) -> str:
    return "\n".join(
        f"- [indexed] {filename}: Description for {filename}"
        for filename in filenames
    )


def _mock_factory(mock: _MockClient):
    async def _factory():
        return mock

    return _factory


class TestSelectRelevantMemories:
    @pytest.mark.asyncio
    async def test_parses_json_array(self):
        mock = _MockClient('["one.md"]')

        result = await select_relevant_memories(
            query="test query",
            manifest=_indexed_manifest("one.md"),
            client_factory=_mock_factory(mock),
        )
        assert result == ["one.md"]

    @pytest.mark.asyncio
    async def test_parses_code_fence(self):
        mock = _MockClient('```json\n["a.md", "b.md"]\n```')

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest("a.md", "b.md"),
            client_factory=_mock_factory(mock),
        )
        assert result == ["a.md", "b.md"]

    @pytest.mark.asyncio
    async def test_caps_at_max(self):
        filenames = [f"e{i}.md" for i in range(10)]
        mock = _MockClient(str(filenames).replace("'", '"'))

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest(*filenames),
            client_factory=_mock_factory(mock),
        )
        assert len(result) == MAX_RECALLED_MEMORIES

    @pytest.mark.asyncio
    async def test_filters_invalid_names(self):
        mock = _MockClient('["real.md", "ghost.md"]')

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest("real.md"),
            client_factory=_mock_factory(mock),
        )
        assert result == ["real.md"]

    @pytest.mark.asyncio
    async def test_already_surfaced_excluded(self):
        mock = _MockClient('["new.md"]')

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest("old.md", "new.md"),
            client_factory=_mock_factory(mock),
            already_surfaced={"old.md"},
        )
        assert result == ["new.md"]

        user_msg = mock.calls[0]["messages"][0]["content"]
        assert "old.md" in user_msg
        assert "Already shown" in user_msg

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        async def _failing_factory():
            raise RuntimeError("API down")

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest("x.md"),
            client_factory=_failing_factory,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(self):
        mock = _MockClient("not json at all")

        result = await select_relevant_memories(
            query="test",
            manifest=_indexed_manifest("x.md"),
            client_factory=_mock_factory(mock),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_recent_tools_in_message(self):
        mock = _MockClient("[]")

        await select_relevant_memories(
            query="test query",
            manifest=_indexed_manifest("t.md"),
            client_factory=_mock_factory(mock),
            recent_tools=["read_file", "grep_search"],
        )

        user_msg = mock.calls[0]["messages"][0]["content"]
        assert "read_file" in user_msg
        assert "grep_search" in user_msg
        assert "Recently used tools" in user_msg


class TestRecallMemoriesForQuery:
    @pytest.mark.asyncio
    async def test_empty_directory(self, mem_dir):
        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=lambda: None,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_end_to_end_with_memory_index(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="user-role",
            description="User is a data scientist",
            content="The user works with pandas daily.",
            metadata={"type": "user"},
        ))
        store.save_entry(MemoryEntry(
            name="project-deadline",
            description="Q3 deadline",
            content="Project must ship by September.",
            metadata={"type": "project"},
        ))
        store.save_entry(MemoryEntry(
            name="not-in-index",
            description="This file is deliberately removed from the index",
            content="This should not be selected.",
            metadata={"type": "reference"},
        ))
        (mem_dir / "MEMORY.md").write_text(
            "- [User Role](user-role.md) - User is a data scientist\n"
            "- [Project Deadline](project-deadline.md) - Q3 deadline\n",
            encoding="utf-8",
        )

        mock = _MockClient('["user-role.md", "project-deadline.md", "not-in-index.md"]')

        result = await recall_memories_for_query(
            query="Help me analyze data",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
        )
        assert len(result) == 2
        filenames = {r.filename for r in result}
        assert filenames == {"user-role.md", "project-deadline.md"}

        user_mem = next(r for r in result if r.filename == "user-role.md")
        assert user_mem.memory_type == MemoryType.USER
        assert "pandas daily" in user_mem.content

        user_msg = mock.calls[0]["messages"][0]["content"]
        assert "not-in-index.md" not in user_msg

    @pytest.mark.asyncio
    async def test_none_selected_returns_empty(self, mem_dir):
        MemoryStore(mem_dir).save_entry(MemoryEntry(
            name="entry",
            description="Some entry",
            content="Content here.",
        ))

        mock = _MockClient("[]")

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_side_query_failure_returns_empty(self, mem_dir):
        MemoryStore(mem_dir).save_entry(MemoryEntry(
            name="entry",
            description="Desc",
            content="Content",
        ))

        async def _failing_factory():
            raise RuntimeError("Network error")

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_failing_factory,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_file_skipped(self, mem_dir):
        MemoryStore(mem_dir).save_entry(MemoryEntry(
            name="existing",
            description="Exists",
            content="Real",
        ))
        (mem_dir / "MEMORY.md").write_text(
            "- [Existing](existing.md) - Exists\n"
            "- [Ghost](ghost.md) - Missing file\n",
            encoding="utf-8",
        )

        mock = _MockClient('["existing.md", "ghost.md"]')

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
        )
        assert len(result) == 1
        assert result[0].filename == "existing.md"

    @pytest.mark.asyncio
    async def test_frontmatter_rename_still_recalls_actual_file(self, mem_dir):
        path = mem_dir / "stable-file.md"
        path.write_text(
            "---\n"
            "name: changed display name\n"
            "description: Still recallable\n"
            "metadata:\n"
            "  type: user\n"
            "---\n\n"
            "Body content\n",
            encoding="utf-8",
        )
        write_memory_index(mem_dir)

        mock = _MockClient('["stable-file.md"]')

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
        )
        assert len(result) == 1
        assert result[0].filename == "stable-file.md"
        assert result[0].file_path == path

    @pytest.mark.asyncio
    async def test_already_surfaced_passed_through(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="a", description="A", content="Content A"))
        store.save_entry(MemoryEntry(name="b", description="B", content="Content B"))
        write_memory_index(mem_dir)

        mock = _MockClient('["b.md"]')

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
            already_surfaced={"a.md"},
        )
        assert len(result) == 1
        assert result[0].filename == "b.md"

    @pytest.mark.asyncio
    async def test_recent_tools_passed_through(self, mem_dir):
        MemoryStore(mem_dir).save_entry(MemoryEntry(
            name="t",
            description="T",
            content="Content T",
        ))

        mock = _MockClient("[]")

        await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
            recent_tools=["bash", "read_file"],
        )
        user_msg = mock.calls[0]["messages"][0]["content"]
        assert "bash" in user_msg

    @pytest.mark.asyncio
    async def test_already_surfaced_hard_filtered_after_selector_response(self, mem_dir):
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(name="a", description="A", content="Content A"))
        store.save_entry(MemoryEntry(name="b", description="B", content="Content B"))
        write_memory_index(mem_dir)

        mock = _MockClient('["a.md", "b.md"]')

        result = await recall_memories_for_query(
            query="anything",
            memory_dir=mem_dir,
            client_factory=_mock_factory(mock),
            already_surfaced={"a.md"},
        )
        assert len(result) == 1
        assert result[0].filename == "b.md"
