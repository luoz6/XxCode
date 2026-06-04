import pytest

from tests.memory.helpers.recall_eval import (
    build_stability_scorecard,
    compute_stability_metrics,
    format_stability_scorecard,
    quality_benchmark_cases,
    reorder_index_content,
)


async def _stability_metrics_for(tmp_path, case_index: int):
    case = quality_benchmark_cases()[case_index]
    return await compute_stability_metrics(case, tmp_path / case.case_id)


async def _stability_scorecard_for_all(tmp_path):
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, tmp_path / case.case_id))
    return build_stability_scorecard(metrics)


@pytest.mark.asyncio
async def test_repeat_consistency_uses_two_identical_runs(tmp_path):
    metrics = await _stability_metrics_for(tmp_path, 0)

    assert metrics.repeat_run_count == 2
    assert metrics.repeat_consistency == 1.0


@pytest.mark.asyncio
async def test_generated_perturbations_preserve_expected_recall(tmp_path):
    metrics = await _stability_metrics_for(tmp_path, 1)

    assert metrics.order_stability == 1.0
    assert metrics.description_robustness == 1.0
    assert metrics.noise_resistance == 1.0


@pytest.mark.asyncio
async def test_stability_scorecard_reports_case_count_and_rates(tmp_path):
    scorecard = await _stability_scorecard_for_all(tmp_path)
    print(format_stability_scorecard(scorecard))

    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.repeat_consistency_rate == 1.0
    assert scorecard.order_stability_rate == 1.0
    assert scorecard.description_robustness_rate == 1.0
    assert scorecard.noise_resistance_rate >= 0.95


def test_stability_scorecard_summary_includes_case_count_and_key_metrics():
    metrics = []
    scorecard = build_stability_scorecard(metrics)

    summary = format_stability_scorecard(scorecard)

    assert "n_cases=0" in summary
    assert "repeat_consistency_rate=0.000" in summary
    assert "noise_resistance_rate=0.000" in summary


def test_reorder_index_content_preserves_blank_lines():
    index_content = (
        "- [Alpha](alpha.md) - First memory\n"
        "\n"
        "- [Beta](beta.md) - Second memory\n"
        "\n"
    )

    reordered = reorder_index_content(index_content)

    assert reordered.count("\n\n") == 2
    assert "- [Beta](beta.md) - Second memory" in reordered.splitlines()[0]
    assert "- [Alpha](alpha.md) - First memory" in reordered
