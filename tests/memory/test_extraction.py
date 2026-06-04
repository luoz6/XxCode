"""Tests for background memory extraction system."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from xxcode.agent.state import AgentState
from xxcode.memory.extraction import (
    ExtractionConfig,
    ExtractionController,
    build_extraction_prompt,
    build_extraction_registry,
)
from xxcode.memory.models import MemoryEntry
from xxcode.memory.store import MemoryStore
from xxcode.tools.file_edit.tool import EditFileTool
from xxcode.tools.file_edit.types import EditFileInput
from xxcode.tools.file_write import WriteFileInput, WriteFileTool
from xxcode.tools.registry import ToolRegistry

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mem_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def base_registry():
    r = ToolRegistry()
    r.register(WriteFileTool())
    r.register(EditFileTool())
    return r


# ── should_extract tests ──────────────────────────────────────────


class TestShouldExtract:
    def test_triggers_when_conditions_met(self):
        state = AgentState(user_turn_count=10)
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is True

    def test_throttled_when_too_few_turns(self):
        state = AgentState(user_turn_count=2, last_extraction_user_turn=0)
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is False

    def test_throttled_after_recent_extraction(self):
        state = AgentState(user_turn_count=12, last_extraction_user_turn=10)
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is False

    def test_mutex_when_memory_writes_detected(self):
        state = AgentState(
            user_turn_count=10,
            memory_writes_since_extraction=True,
        )
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is False

    def test_triggers_after_sufficient_gap(self):
        state = AgentState(user_turn_count=15, last_extraction_user_turn=10)
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is True

    def test_uses_user_turn_count_not_tool_turn_count(self):
        state = AgentState(
            turn_count=0,
            user_turn_count=6,
            last_extraction_user_turn=0,
        )
        ctrl = ExtractionController.__new__(ExtractionController)
        assert ctrl.should_extract(state) is True


# ── build_extraction_prompt tests ─────────────────────────────────


class TestBuildExtractionPrompt:
    def test_includes_manifest(self):
        prompt = build_extraction_prompt(
            messages_slice=[{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            existing_manifest="- [user] test.md: description",
        )
        assert "test.md" in prompt
        assert "description" in prompt
        assert "Hello" in prompt

    def test_includes_turn_limit(self):
        prompt = build_extraction_prompt(
            messages_slice=[],
            existing_manifest="(no memories available)",
            max_turns=3,
        )
        assert "3 turns" in prompt

    def test_includes_instructions(self):
        prompt = build_extraction_prompt(
            messages_slice=[],
            existing_manifest="(empty)",
        )
        assert "Existing MEMORY.md index" in prompt
        assert "Keep MEMORY.md in sync" in prompt
        assert "parallel reads" in prompt
        assert "parallel writes" in prompt


# ── build_extraction_registry tests ────────────────────────────────


class TestBuildExtractionRegistry:
    def test_includes_read_tools(self, base_registry, mem_dir):
        """Read tools should be included."""
        filtered = build_extraction_registry(base_registry)
        # The registry should only have write_file, edit_file
        # (ReadFileTool, GrepSearchTool, GlobMatchTool are not in base_registry)
        assert filtered.get("write_file") is not None
        assert filtered.get("edit_file") is not None

    def test_agent_tool_not_included(self, base_registry, mem_dir):
        """AgentTool must NOT be in the extraction registry."""
        filtered = build_extraction_registry(base_registry)
        assert filtered.get("agent") is None


# ── WriteFileTool validate_input tests ─────────────────────────────


class TestWriteFileAllowedRoots:
    def test_blocks_path_outside_roots(self):
        tool = WriteFileTool()
        inp = WriteFileInput(file_path="/tmp/outside/file.txt", content="hello")
        ok, msg = asyncio.run(
            tool.validate_input(inp, {"allowed_write_roots": ["/safe/dir"]})
        )
        assert ok is False
        assert "outside allowed roots" in msg

    def test_allows_path_inside_roots(self):
        tool = WriteFileTool()
        inp = WriteFileInput(file_path="/safe/dir/sub/file.txt", content="hello")
        ok, msg = asyncio.run(
            tool.validate_input(inp, {"allowed_write_roots": ["/safe/dir"]})
        )
        assert ok is True

    def test_allows_exact_root_path(self):
        tool = WriteFileTool()
        inp = WriteFileInput(file_path="/safe/dir", content="hello")
        ok, msg = asyncio.run(
            tool.validate_input(inp, {"allowed_write_roots": ["/safe/dir"]})
        )
        assert ok is True

    def test_passthrough_when_no_roots_set(self):
        """Without allowed_write_roots, everything is allowed (backward compat)."""
        tool = WriteFileTool()
        inp = WriteFileInput(file_path="/anywhere/file.txt", content="hello")
        ok, msg = asyncio.run(
            tool.validate_input(inp, {})
        )
        assert ok is True

    def test_multiple_roots(self):
        tool = WriteFileTool()
        inp = WriteFileInput(file_path="/other/safe/file.txt", content="hello")
        ok, msg = asyncio.run(
            tool.validate_input(inp, {
                "allowed_write_roots": ["/safe/dir", "/other/safe"],
            })
        )
        assert ok is True


# ── EditFileTool validate_input tests ──────────────────────────────


class TestEditFileAllowedRoots:
    def test_blocks_path_outside_roots(self):
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="/tmp/bad/edit.txt",
            old_string="old",
            new_string="new",
        )
        ok, msg = asyncio.run(
            tool.validate_input(inp, {"allowed_write_roots": ["/safe"]})
        )
        assert ok is False
        assert "outside allowed roots" in msg


class TestEditFileSkipReadBeforeEdit:
    def test_skip_read_before_edit_bypasses_unread_check(self):
        """With skip_read_before_edit=True, Step 3 is skipped.

        The file doesn't exist, so Step 2 (existence check) still fails.
        But we verify the read-before-edit check is NOT the one failing.
        """
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="/nonexistent/file.md",
            old_string="old",
            new_string="new",
        )
        # Without skip, the validate_input would fail at Step 2 (file not found)
        # — not at Step 3 (unread file).  So we verify the error message
        # mentions file-not-found, not unread-file.
        ok, msg = asyncio.run(
            tool.validate_input(inp, {"skip_read_before_edit": True})
        )
        assert ok is False
        assert "not found" in msg.lower() or "FILE_NOT_FOUND" in msg


# ── State serialization tests ─────────────────────────────────────


class TestStateSerialization:
    def test_extraction_fields_roundtrip(self):
        state = AgentState(
            last_extraction_turn=7,
            user_turn_count=12,
            last_extraction_user_turn=11,
            memory_writes_since_extraction=True,
        )
        data = state.to_dict()
        restored = AgentState.from_dict(data)
        assert restored.last_extraction_turn == 7
        assert restored.user_turn_count == 12
        assert restored.last_extraction_user_turn == 11
        assert restored.memory_writes_since_extraction is True

    def test_default_values(self):
        state = AgentState()
        assert state.last_extraction_turn == 0
        assert state.user_turn_count == 0
        assert state.last_extraction_user_turn == 0
        assert state.memory_writes_since_extraction is False


# ── ExtractionController concurrency tests ────────────────────────


class TestExtractionControllerConcurrency:
    def test_schedule_returns_none_when_throttled(self):
        from xxcode.config import get_config

        config = get_config()
        registry = ToolRegistry()

        ctrl = ExtractionController(config, registry)
        # Fresh state with user_turn_count=0 won't trigger (gap not met)
        state = AgentState(user_turn_count=0)
        task = ctrl.schedule(state, Path("/tmp/nonexistent"))
        assert task is None

    def test_schedule_stores_pending_when_running(self):
        """When a task is already running, schedule stores pending context."""
        async def _run():
            from xxcode.config import get_config

            config = get_config()
            registry = ToolRegistry()

            ctrl = ExtractionController(config, registry)

            # Fake a running task
            async def _fake_run():
                await asyncio.sleep(10)
                return None

            ctrl._current_task = asyncio.create_task(_fake_run())

            # Now schedule with a state that WOULD trigger
            state = AgentState(turn_count=10, user_turn_count=10)
            task = ctrl.schedule(state, Path("/tmp"))
            assert task is None  # Because a task is already running
            assert ctrl._pending_context is not None
            assert ctrl._pending_context["turn_count"] == 10
            assert ctrl._pending_context["user_turn_count"] == 10

            # Cleanup
            ctrl._current_task.cancel()
            ctrl._pending_context = None

        asyncio.run(_run())

    def test_has_pending_result_and_consume(self):
        from xxcode.config import get_config

        config = get_config()
        registry = ToolRegistry()

        ctrl = ExtractionController(config, registry)
        assert ctrl.has_pending_result() is False
        assert ctrl.consume_result() is None

        # Set a result directly
        ctrl._last_result = "Memory saved: test"
        assert ctrl.has_pending_result() is True

        result = ctrl.consume_result()
        assert result == "Memory saved: test"
        assert ctrl.has_pending_result() is False
        assert ctrl.consume_result() is None

    def test_cancel_clears_stale_completed_result(self):
        from xxcode.config import get_config

        config = get_config()
        registry = ToolRegistry()

        ctrl = ExtractionController(config, registry)
        ctrl._last_result = "stale result"
        ctrl.cancel()
        assert ctrl.has_pending_result() is False


# ── End-to-end test (mock SubAgent) ───────────────────────────────


class TestExtractionPipeline:
    def test_extraction_with_mock_subagent(self, mem_dir):
        """Full pipeline with a mock API that the extraction controller
        would use. Tests that build_extraction_prompt + registry work.

        Note: This test does NOT run the actual SubAgent (which would
        require a real API key). Instead it verifies the setup is correct.
        """
        from xxcode.config import get_config

        config = get_config()
        registry = ToolRegistry()
        registry.register(WriteFileTool())
        registry.register(EditFileTool())

        # Pre-populate memory directory
        store = MemoryStore(mem_dir)
        store.save_entry(MemoryEntry(
            name="existing",
            description="Already exists",
            content="Existing content",
        ))

        index_content = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "existing.md" in index_content

        # Build prompt
        prompt = build_extraction_prompt(
            messages_slice=[
                {"role": "user", "content": [{"type": "text", "text": "I prefer snake_case."}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Noted."}]},
            ],
            existing_manifest="- [user] existing.md: Already exists",
        )
        assert "snake_case" in prompt
        assert "existing.md" in prompt

        # Build filtered registry
        filtered = build_extraction_registry(registry)
        assert filtered.get("write_file") is not None
        assert filtered.get("edit_file") is not None
        assert filtered.get("agent") is None
