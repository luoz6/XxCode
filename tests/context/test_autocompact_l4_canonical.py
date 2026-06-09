from xxcode.agent.state import AgentState
from xxcode.tools.file_edit.types import FileStateEntry


def test_recent_read_files_skip_partial_views_and_sort_by_timestamp():
    from xxcode.agent.loop import _recent_read_files_for_post_compact

    state = AgentState()
    state.read_file_state = {
        "/old.py": FileStateEntry(content="old", timestamp=1.0),
        "/partial.py": FileStateEntry(content="partial", timestamp=3.0, is_partial_view=True),
        "/new.py": FileStateEntry(content="new", timestamp=2.0),
    }

    recent = _recent_read_files_for_post_compact(state, limit=5)

    assert recent == [
        {"path": "/old.py", "content": "old"},
        {"path": "/new.py", "content": "new"},
    ]


def test_l4_runtime_cleanup_clears_regions_cache_edits_and_breakpoints(tmp_path):
    from xxcode.agent.loop import CoreExecutionEngine
    from xxcode.config import Config
    from xxcode.context.micro import CacheEdit

    engine = CoreExecutionEngine(
        Config(
            api_key="key",
            api_base_url="https://example.test",
            api_model="claude-sonnet-4",
            cwd=tmp_path,
            session_dir=tmp_path / "sessions",
        )
    )
    state = AgentState()
    state.cache_breakpoints = {1, 2}
    engine._l3_regions = [object()]
    engine._cache_edit_state.pending.append(CacheEdit(tool_use_id="tool-1"))
    engine._cache_edit_state.pinned.append(CacheEdit(tool_use_id="tool-2"))

    engine._clear_runtime_compression_state_after_history_replace(state)

    assert engine._l3_regions == []
    assert engine._cache_edit_state.pending == []
    assert engine._cache_edit_state.pinned == []
    assert engine._cache_edit_state.consumed_in_flight == []
    assert state.cache_breakpoints == set()
