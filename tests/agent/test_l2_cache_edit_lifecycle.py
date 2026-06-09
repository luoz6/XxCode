from xxcode.agent.loop import CoreExecutionEngine
from xxcode.config import Config
from xxcode.context.micro import CacheEdit


def _engine(tmp_path, model="claude-sonnet-4"):
    return CoreExecutionEngine(
        Config(
            api_key="key",
            api_base_url="https://example.test",
            api_model=model,
            cwd=tmp_path,
            session_dir=tmp_path / "sessions",
            anthropic_cache_edits_enabled=True,
        )
    )


def test_cache_edits_success_moves_in_flight_to_pinned(tmp_path):
    engine = _engine(tmp_path)
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))

    options = engine._consume_cache_edits_for_request({"tool-1"})
    assert [edit.tool_use_id for edit in options.anthropic_cache_edits] == ["tool-1"]
    assert engine._cache_edit_state.pending == []
    assert [edit.tool_use_id for edit in engine._cache_edit_state.consumed_in_flight] == ["tool-1"]

    engine._pin_consumed_cache_edits()

    assert engine._cache_edit_state.consumed_in_flight == []
    assert [edit.tool_use_id for edit in engine._cache_edit_state.pinned] == ["tool-1"]


def test_cache_edits_failure_restores_pending(tmp_path):
    engine = _engine(tmp_path)
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))

    engine._consume_cache_edits_for_request({"tool-1"})
    engine._restore_in_flight_cache_edits()

    assert [edit.tool_use_id for edit in engine._cache_edit_state.pending] == ["tool-1"]
    assert engine._cache_edit_state.consumed_in_flight == []


def test_absent_projected_targets_are_retired(tmp_path):
    engine = _engine(tmp_path)
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))
    engine._cache_edit_state.pinned.append(CacheEdit(tool_use_id="tool-2"))

    options = engine._consume_cache_edits_for_request(set())

    assert options.anthropic_cache_edits in (None, [])
    assert engine._cache_edit_state.pending == []
    assert engine._cache_edit_state.pinned == []


def test_non_anthropic_models_clear_runtime_cache_edits(tmp_path):
    engine = _engine(tmp_path, model="deepseek-chat")
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))
    engine._cache_edit_state.pinned.append(CacheEdit(tool_use_id="tool-2"))

    options = engine._consume_cache_edits_for_request({"tool-1", "tool-2"})

    assert options.anthropic_cache_edits is None
    assert engine._cache_edit_state.pending == []
    assert engine._cache_edit_state.pinned == []
