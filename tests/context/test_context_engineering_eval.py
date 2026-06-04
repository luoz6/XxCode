import pytest

from tests.context.helpers.context_eval import (
    CompressionDiagnostics,
    ContextEvalCase,
    ContextEvalMetrics,
    ContextSnapshot,
    RecallDiagnostics,
    build_context_eval_scorecard,
    compute_context_eval_metrics,
    format_context_eval_scorecard,
    render_flattened_snapshot,
    run_context_case,
    semantic_benchmark_cases,
)


_DEFAULT_BUDGET = {
    "soft_limit_tokens": 4000,
    "hard_limit_tokens": 8000,
}


def _make_context_case(
    *,
    case_id: str,
    scenario: str,
    messages: list[dict],
    target_turn_index: int,
    cwd_files: dict[str, str] | None = None,
    memory_index_content: str = "",
    memory_files: dict[str, str] | None = None,
    expected_compression_level: int = 0,
    expected_present: list[str] | None = None,
    expected_absent: list[str] | None = None,
    expected_recent_present: list[str] | None = None,
    expected_stale_absent: list[str] | None = None,
    expected_order: list[tuple[str, str]] | None = None,
    required_sections: list[str] | None = None,
    expected_recall_diagnostics: RecallDiagnostics | None = None,
    expected_compression_diagnostics: CompressionDiagnostics | None = None,
    budget_expectation: dict[str, int] | None = None,
) -> ContextEvalCase:
    return ContextEvalCase(
        case_id=case_id,
        scenario=scenario,
        cwd_files=cwd_files or {},
        messages=messages,
        memory_index_content=memory_index_content,
        memory_files=memory_files or {},
        target_turn_index=target_turn_index,
        expected_compression_level=expected_compression_level,
        expected_present=expected_present or [],
        expected_absent=expected_absent or [],
        expected_recent_present=expected_recent_present or [],
        expected_stale_absent=expected_stale_absent or [],
        expected_order=expected_order or [],
        required_sections=required_sections or [],
        expected_recall_diagnostics=expected_recall_diagnostics
        or RecallDiagnostics(index_injected=True, recalled_count=0, recall_empty=True),
        expected_compression_diagnostics=expected_compression_diagnostics
        or CompressionDiagnostics(
            compression_used=False,
            level_reached=expected_compression_level,
            summary_injected=False,
        ),
        budget_expectation=budget_expectation or dict(_DEFAULT_BUDGET),
    )


def _simple_case() -> ContextEvalCase:
    return _make_context_case(
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
        target_turn_index=1,
        expected_present=["Do not modify settings.py"],
        expected_recent_present=["Do not modify settings.py"],
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


def _memory_case() -> ContextEvalCase:
    return _make_context_case(
        case_id="memory-injection",
        scenario="Relevant memory is recalled and injected before the current user turn.",
        cwd_files={"CLAUDE.md": "Project instructions."},
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Let's inspect the data pipeline."}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Please plan the dataframe analysis flow."}],
            },
        ],
        memory_index_content=(
            "- [Python Style](python-style.md) - User prefers pandas dataframe analysis\n"
            "- [Release Note](release-note.md) - Release process archive\n"
        ),
        memory_files={
            "python-style.md": (
                "---\nmetadata:\n  type: user\n---\n\n"
                "Use pandas for dataframe-style analysis.\n"
            ),
            "release-note.md": (
                "---\nmetadata:\n  type: reference\n---\n\n"
                "Archive note.\n"
            ),
        },
        target_turn_index=1,
        expected_present=[
            "Contents of",
            "Use pandas for dataframe-style analysis.",
            "Please plan the dataframe analysis flow.",
        ],
        expected_recent_present=["Please plan the dataframe analysis flow."],
        expected_order=[
            ("Use pandas for dataframe-style analysis.", "Please plan the dataframe analysis flow."),
        ],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=1,
            recall_empty=False,
        ),
    )


@pytest.mark.asyncio
async def test_run_context_case_injects_memory_index_and_recalled_memory(tmp_path):
    case = _memory_case()

    snapshot = await run_context_case(
        case,
        memory_dir=tmp_path / "memory",
        cwd=tmp_path / "cwd",
    )

    assert "Contents of" in snapshot.flattened_text_snapshot
    assert "Use pandas for dataframe-style analysis." in snapshot.flattened_text_snapshot
    assert snapshot.recall_diagnostics.index_injected is True
    assert snapshot.recall_diagnostics.recalled_count == 1
    assert snapshot.recall_diagnostics.recall_empty is False


def _compressing_case() -> ContextEvalCase:
    noisy_result = (
        "Collecting demo-package\n"
        "Downloading demo-package\n"
        "Successfully installed demo-package\n\n"
        + ("Collecting demo-package\nDownloading demo-package\n" * 80)
    )
    return _make_context_case(
        case_id="compression-budget",
        scenario="Noisy historical tool output is compressed while recent context remains.",
        cwd_files={"CLAUDE.md": "Respect recent task context."},
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "run_shell",
                        "input": {"command": "pip install demo-package"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": noisy_result,
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I checked the install output."}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Keep processor.py as the current focus."}],
            },
        ],
        target_turn_index=3,
        expected_compression_level=1,
        expected_present=["Keep processor.py as the current focus."],
        expected_absent=["Collecting demo-package\nDownloading demo-package\nCollecting demo-package"],
        expected_recent_present=["Keep processor.py as the current focus."],
        expected_stale_absent=["Successfully installed demo-package"],
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=True,
            level_reached=1,
            summary_injected=False,
        ),
        budget_expectation={
            "soft_limit_tokens": 200,
            "hard_limit_tokens": 400,
        },
    )


@pytest.mark.asyncio
async def test_run_context_case_applies_compression_and_budget_checks(tmp_path):
    case = _compressing_case()

    snapshot = await run_context_case(
        case,
        memory_dir=tmp_path / "memory",
        cwd=tmp_path / "cwd",
    )

    assert "Keep processor.py as the current focus." in snapshot.flattened_text_snapshot
    assert "Collecting demo-package\nDownloading demo-package\nCollecting demo-package" not in snapshot.flattened_text_snapshot
    assert snapshot.compression_diagnostics.compression_used is True
    assert snapshot.compression_diagnostics.level_reached == 1
    assert snapshot.token_counts["prepared_messages_tokens"] < case.budget_expectation["soft_limit_tokens"]


def test_compute_context_eval_metrics_counts_required_present_absent_and_budget():
    case = _compressing_case()
    snapshot = ContextSnapshot(
        case_id=case.case_id,
        system_prompt="system",
        prepared_messages=[],
        flattened_text_snapshot="Keep processor.py as the current focus.",
        structured_snapshot_view=None,
        token_counts={
            "prepared_messages_tokens": 120,
            "flattened_snapshot_tokens": 150,
        },
        recall_diagnostics=case.expected_recall_diagnostics,
        compression_diagnostics=case.expected_compression_diagnostics,
    )

    metrics = compute_context_eval_metrics(case, snapshot)

    assert metrics == ContextEvalMetrics(
        case_id=case.case_id,
        required_content_hit=1.0,
        required_order_pass=1.0,
        section_presence_pass=1.0,
        recent_context_preserved=1.0,
        stale_content_exclusion_pass=1.0,
        forbidden_content_absence_pass=1.0,
        budget_pass=1.0,
        recall_activation_pass=1.0,
        compression_activation_pass=1.0,
        snapshot_validity_pass=1.0,
    )


@pytest.mark.asyncio
async def test_semantic_benchmark_cases_produce_passing_scorecard(tmp_path):
    metrics = []
    for case in semantic_benchmark_cases():
        snapshot = await run_context_case(
            case,
            memory_dir=tmp_path / case.case_id / "memory",
            cwd=tmp_path / case.case_id / "cwd",
        )
        metrics.append(compute_context_eval_metrics(case, snapshot))

    scorecard = build_context_eval_scorecard(metrics)
    print(format_context_eval_scorecard(scorecard))

    assert scorecard.n_cases == len(semantic_benchmark_cases())
    assert scorecard.required_content_hit_rate == 1.0
    assert scorecard.forbidden_content_absence_rate == 1.0
    assert scorecard.budget_pass_rate == 1.0
    assert scorecard.snapshot_validity_rate == 1.0


def test_context_eval_scorecard_summary_includes_key_rates():
    summary = format_context_eval_scorecard(
        build_context_eval_scorecard([
            ContextEvalMetrics(
                case_id="demo",
                required_content_hit=1.0,
                required_order_pass=1.0,
                section_presence_pass=1.0,
                recent_context_preserved=1.0,
                stale_content_exclusion_pass=1.0,
                forbidden_content_absence_pass=1.0,
                budget_pass=1.0,
                recall_activation_pass=1.0,
                compression_activation_pass=1.0,
                snapshot_validity_pass=1.0,
            )
        ])
    )

    assert "n_cases=1" in summary
    assert "required_content_hit_rate=1.000" in summary
    assert "budget_pass_rate=1.000" in summary


def test_optional_metric_aggregates_skip_none_values():
    scorecard = build_context_eval_scorecard([
        ContextEvalMetrics(
            case_id="one",
            required_content_hit=1.0,
            required_order_pass=1.0,
            section_presence_pass=1.0,
            recent_context_preserved=None,
            stale_content_exclusion_pass=None,
            forbidden_content_absence_pass=1.0,
            budget_pass=1.0,
            recall_activation_pass=1.0,
            compression_activation_pass=1.0,
            snapshot_validity_pass=1.0,
        )
    ])

    assert scorecard.recent_context_preservation_rate == 0.0
    assert scorecard.stale_content_exclusion_rate == 0.0
