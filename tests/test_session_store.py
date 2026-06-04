"""Tests for session persistence metadata."""

import json

from xxcode.agent.state import AgentState
from xxcode.skills.persistence import SkillPersistence
from xxcode.ui.session import SessionMeta, SessionStore


def test_save_preserves_existing_turn_count(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-1"
    meta = SessionMeta(session_id=session_id, turn_count=7)

    store.save(
        session_id,
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        meta=meta,
    )

    loaded = store.load_meta(session_id)
    assert loaded is not None
    assert loaded.message_count == 1
    assert loaded.turn_count == 7


def test_save_accepts_explicit_turn_count(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-2"

    store.save(
        session_id,
        [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
        turn_count=3,
    )

    loaded = store.load_meta(session_id)
    assert loaded is not None
    assert loaded.message_count == 2
    assert loaded.turn_count == 3


def test_save_state_updates_meta_from_agent_state(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-3"
    state = AgentState(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ],
        turn_count=5,
    )

    store.save(session_id, [{"role": "user", "content": []}], turn_count=1)
    store.save_state(session_id, state)

    loaded = store.load_meta(session_id)
    assert loaded is not None
    assert loaded.message_count == 2
    assert loaded.turn_count == 5

    raw = json.loads((tmp_path / f"{session_id}.state.json").read_text(encoding="utf-8"))
    assert raw["generation"]
    assert raw["state"]["turn_count"] == 5


def test_save_state_preserves_existing_skill_recovery_snapshot(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-3b"
    state = AgentState(messages=[], turn_count=2)
    persistence = SkillPersistence()
    persistence.record_invocation("main", "review", "/review", "prompt", turn_count=2)

    store.save_state_with_recovery(session_id, state, persistence.export_snapshot())
    store.save_state(session_id, state)

    snapshot = store.load_skill_recovery(session_id)
    assert snapshot is not None
    assert snapshot["agent_scopes"]["main"]["review"]["content"] == "prompt"


def test_save_state_preserves_existing_task_runtime_snapshot(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-3c"
    state = AgentState(messages=[], turn_count=2)
    task_snapshot = [
        {
            "task_id": "task-1",
            "parent_task_id": None,
            "parent_scope_id": "main",
            "worker_label": "Task 1",
            "description": "desc",
            "agent_type": "general-purpose",
            "reusable": False,
            "status": "completed",
            "created_at": 1.0,
            "updated_at": 1.0,
            "result_text": "done",
            "error_text": "",
            "result_file": "",
            "error_file": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_use_count": 0,
            "duration_ms": 0,
            "termination_reason": "",
        }
    ]

    store.save_state_with_recovery(session_id, state, {"version": 1, "agent_scopes": {}}, task_snapshot)
    store.save_state(session_id, state)

    loaded_tasks = store.load_task_runtime_snapshot(session_id)
    assert loaded_tasks is not None
    assert loaded_tasks[0]["task_id"] == "task-1"


def test_save_state_with_recovery_round_trips_and_validates_generation(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-4"
    state = AgentState(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        turn_count=1,
    )
    persistence = SkillPersistence()
    persistence.record_invocation("main", "review", "/review", "prompt", turn_count=1)

    store.save_state_with_recovery(session_id, state, persistence.export_snapshot())

    snapshot = store.load_skill_recovery(session_id)
    assert snapshot is not None
    assert snapshot["version"] == 1
    assert snapshot["agent_scopes"]["main"]["review"]["name"] == "review"

    recovery_path = tmp_path / f"{session_id}.skill-recovery.json"
    raw = json.loads(recovery_path.read_text(encoding="utf-8"))
    raw["generation"] = "mismatch"
    recovery_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    assert store.load_skill_recovery(session_id) is None


def test_load_state_still_succeeds_when_recovery_generation_mismatches(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-4a"
    state = AgentState(
        messages=[{"role": "user", "content": [{"type": "text", "text": "keep state"}]}],
        turn_count=6,
    )
    persistence = SkillPersistence()
    persistence.record_invocation("main", "review", "/review", "prompt", turn_count=6)

    store.save_state_with_recovery(session_id, state, persistence.export_snapshot())

    recovery_path = tmp_path / f"{session_id}.skill-recovery.json"
    raw = json.loads(recovery_path.read_text(encoding="utf-8"))
    raw["generation"] = "mismatch"
    recovery_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    loaded_state = store.load_state(session_id)
    assert loaded_state is not None
    assert loaded_state.turn_count == 6
    assert loaded_state.messages[0]["content"][0]["text"] == "keep state"
    assert store.load_skill_recovery(session_id) is None


def test_load_skill_recovery_ignores_malformed_json(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-4b"
    state = AgentState(messages=[], turn_count=0)

    store.save_state_with_recovery(session_id, state, {"version": 1, "agent_scopes": {}})

    (tmp_path / f"{session_id}.skill-recovery.json").write_text("{broken", encoding="utf-8")

    assert store.load_skill_recovery(session_id) is None


def test_load_skill_recovery_ignores_malformed_record_values(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-4c"
    state = AgentState(messages=[], turn_count=0)
    snapshot = {
        "version": 1,
        "agent_scopes": {
            "main": {
                "review": {
                    "name": "review",
                    "path": "/review",
                    "content": "prompt",
                    "invoked_at": "abc",
                    "agent_scope": "main",
                    "last_turn_index": 1,
                    "invocation_count": 1,
                }
            }
        },
    }

    store.save_state_with_recovery(session_id, state, snapshot)

    loaded_snapshot = store.load_skill_recovery(session_id)
    assert loaded_snapshot is not None

    persistence = SkillPersistence()
    persistence.import_snapshot(loaded_snapshot)
    assert persistence.build_recovery_attachment("main") is None


def test_load_meta_ignores_malformed_json(tmp_path):
    store = SessionStore(tmp_path)
    (tmp_path / "broken.meta.json").write_text("{broken", encoding="utf-8")

    assert store.load_meta("broken") is None


def test_load_state_ignores_malformed_json(tmp_path):
    store = SessionStore(tmp_path)
    (tmp_path / "broken.state.json").write_text("{broken", encoding="utf-8")

    assert store.load_state("broken") is None


def test_load_task_runtime_snapshot_ignores_malformed_json(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-4d"
    state = AgentState(messages=[], turn_count=0)

    store.save_state_with_recovery(session_id, state, {"version": 1, "agent_scopes": {}})
    (tmp_path / f"{session_id}.tasks.json").write_text("{broken", encoding="utf-8")

    assert store.load_task_runtime_snapshot(session_id) is None


def test_load_skips_truncated_jsonl_lines(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-truncated"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": []}, ensure_ascii=False) + "\n{broken\n",
        encoding="utf-8",
    )

    assert store.load(session_id) == [{"role": "user", "content": []}]


def test_atomic_write_failure_preserves_existing_file_and_cleans_temp(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    session_id = "session-atomic"
    original_messages = [{"role": "user", "content": [{"type": "text", "text": "first"}]}]
    store.save(session_id, original_messages, turn_count=1)

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("xxcode.ui.session.os.replace", _boom)

    try:
        store.save(
            session_id,
            [{"role": "user", "content": [{"type": "text", "text": "second"}]}],
            turn_count=2,
        )
    except OSError as exc:
        assert "replace failed" in str(exc)
    else:
        raise AssertionError("Expected OSError from atomic replace failure")

    assert store.load(session_id) == original_messages
    assert not list(tmp_path.glob("*.tmp"))


def test_delete_removes_skill_recovery_file(tmp_path):
    store = SessionStore(tmp_path)
    session_id = "session-5"
    state = AgentState(messages=[], turn_count=0)

    store.save_state_with_recovery(session_id, state, {"version": 1, "agent_scopes": {}})
    assert (tmp_path / f"{session_id}.skill-recovery.json").exists()
    assert (tmp_path / f"{session_id}.skill-recovery.commit.json").exists()

    store.delete(session_id)
    assert not (tmp_path / f"{session_id}.skill-recovery.json").exists()
    assert not (tmp_path / f"{session_id}.skill-recovery.commit.json").exists()


# ── /resume command round-trip tests ──────────────────────────────────


def test_full_resume_roundtrip_preserves_all_state(tmp_path):
    """save_state → load_state must round-trip all critical resume fields."""
    store = SessionStore(tmp_path)
    session_id = "resume-1"
    state = AgentState(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
        ],
        turn_count=3,
        total_input_tokens=100,
        total_output_tokens=50,
        last_query="hello",
    )

    store.save_state(session_id, state)
    loaded = store.load_state(session_id)

    assert loaded is not None
    assert len(loaded.messages) == 2
    assert loaded.turn_count == 3
    assert loaded.total_input_tokens == 100
    assert loaded.total_output_tokens == 50
    assert loaded.last_query == "hello"
    assert loaded.messages[0]["content"][0]["text"] == "hello"
    assert loaded.messages[1]["content"][0]["text"] == "hi there"


def test_load_state_nonexistent_returns_none(tmp_path):
    """Loading a session ID that doesn't exist must return None."""
    store = SessionStore(tmp_path)
    assert store.load_state("nonexistent-id") is None


def test_resume_roundtrip_yolo_mode(tmp_path):
    """YOLO mode must survive a full save→load round-trip."""
    store = SessionStore(tmp_path)
    session_id = "resume-yolo"
    state = AgentState(
        messages=[{"role": "user", "content": [{"type": "text", "text": "cmd"}]}],
        turn_count=1,
    )
    state.permission_state.yolo_mode = True

    store.save_state(session_id, state)
    loaded = store.load_state(session_id)

    assert loaded is not None
    assert loaded.permission_state.yolo_mode is True
