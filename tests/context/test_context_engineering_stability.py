import pytest

from tests.context.helpers.context_eval import (
    _with_memory_index_reordered,
    _with_stale_history_noise,
    build_context_eval_scorecard,
    compute_context_eval_metrics,
    run_context_case,
    stability_benchmark_cases,
)


@pytest.mark.asyncio
async def test_memory_index_reorder_preserves_required_snapshot_content(tmp_path):
    case = stability_benchmark_cases()[0]

    baseline = await run_context_case(
        case,
        memory_dir=tmp_path / "baseline" / "memory",
        cwd=tmp_path / "baseline" / "cwd",
    )
    reordered = await run_context_case(
        _with_memory_index_reordered(case),
        memory_dir=tmp_path / "reordered" / "memory",
        cwd=tmp_path / "reordered" / "cwd",
    )

    assert "Use pandas for dataframe-style analysis." in baseline.flattened_text_snapshot
    assert "Use pandas for dataframe-style analysis." in reordered.flattened_text_snapshot


@pytest.mark.asyncio
async def test_stale_history_noise_insertion_keeps_recent_context_visible(tmp_path):
    case = stability_benchmark_cases()[1]

    noisy = await run_context_case(
        _with_stale_history_noise(case),
        memory_dir=tmp_path / "noise" / "memory",
        cwd=tmp_path / "noise" / "cwd",
    )

    assert "Keep processor.py as the current focus." in noisy.flattened_text_snapshot


@pytest.mark.asyncio
async def test_stability_benchmark_cases_keep_scorecard_green(tmp_path):
    metrics = []
    for case in stability_benchmark_cases():
        snapshot = await run_context_case(
            case,
            memory_dir=tmp_path / case.case_id / "memory",
            cwd=tmp_path / case.case_id / "cwd",
        )
        metrics.append(compute_context_eval_metrics(case, snapshot))

    scorecard = build_context_eval_scorecard(metrics)

    assert scorecard.required_content_hit_rate == 1.0
    assert scorecard.recent_context_preservation_rate >= 0.95
    assert scorecard.stale_content_exclusion_rate >= 0.95
