import pytest

from tests.context.helpers.context_eval import (
    CompressionDiagnostics,
    ContextEvalCase,
    ContextSnapshot,
    RecallDiagnostics,
    render_flattened_snapshot,
    run_context_case,
)


def _simple_case() -> ContextEvalCase:
    return ContextEvalCase(
        case_id="constraint-only",
        scenario="Preserve a simple user constraint in the flattened snapshot.",
        cwd_files={"CLAUDE.md": "Always preserve explicit user constraints."},
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I will inspect the repository."}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Do not modify settings.py"}],
            },
        ],
        memory_index_content="",
        memory_files={},
        target_turn_index=1,
        expected_compression_level=0,
        expected_present=["Do not modify settings.py"],
        expected_absent=[],
        expected_recent_present=["Do not modify settings.py"],
        expected_stale_absent=[],
        expected_order=[],
        required_sections=[],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=0,
            recall_empty=True,
        ),
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
        budget_expectation={
            "soft_limit_tokens": 4000,
            "hard_limit_tokens": 8000,
        },
    )


def test_render_flattened_snapshot_uses_stable_double_newline_format():
    snapshot = ContextSnapshot(
        case_id="render-demo",
        system_prompt="system prompt text",
        prepared_messages=[
            {"role": "user", "content": [{"type": "text", "text": "first user"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "assistant reply"}]},
        ],
        flattened_text_snapshot="",
        structured_snapshot_view=None,
        token_counts={
            "prepared_messages_tokens": 10,
            "flattened_snapshot_tokens": 12,
        },
        recall_diagnostics=RecallDiagnostics(
            index_injected=False,
            recalled_count=0,
            recall_empty=True,
        ),
        compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
    )

    flattened = render_flattened_snapshot(snapshot.system_prompt, snapshot.prepared_messages)

    assert flattened.startswith("[SYSTEM PROMPT]\nsystem prompt text")
    assert "\n\n[MESSAGE role=user]\nfirst user" in flattened
    assert "\n\n[MESSAGE role=assistant]\nassistant reply" in flattened


@pytest.mark.asyncio
async def test_run_context_case_returns_snapshot_and_preserves_constraint(tmp_path):
    case = _simple_case()

    snapshot = await run_context_case(
        case,
        memory_dir=tmp_path / "memory",
        cwd=tmp_path / "cwd",
    )

    assert isinstance(snapshot, ContextSnapshot)
    assert "Do not modify settings.py" in snapshot.flattened_text_snapshot
    assert snapshot.recall_diagnostics.index_injected is True
    assert snapshot.compression_diagnostics.level_reached == 0
