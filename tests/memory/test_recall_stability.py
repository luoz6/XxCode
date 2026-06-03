import pytest

from tests.memory.helpers.recall_eval import (
    build_stability_scorecard,
    compute_stability_metrics,
    quality_benchmark_cases,
)


@pytest.mark.asyncio
async def test_repeat_consistency_uses_two_identical_runs(tmp_path):
    case = quality_benchmark_cases()[0]

    metrics = await compute_stability_metrics(case, tmp_path / case.case_id)

    assert metrics.repeat_run_count == 2
    assert metrics.repeat_consistency == 1.0


@pytest.mark.asyncio
async def test_generated_perturbations_preserve_expected_recall(tmp_path):
    case = quality_benchmark_cases()[1]

    metrics = await compute_stability_metrics(case, tmp_path / case.case_id)

    assert metrics.order_stability == 1.0
    assert metrics.description_robustness == 1.0
    assert metrics.noise_resistance == 1.0


@pytest.mark.asyncio
async def test_stability_scorecard_reports_case_count_and_rates(tmp_path):
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, tmp_path / case.case_id))

    scorecard = build_stability_scorecard(metrics)

    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.repeat_consistency_rate == 1.0
    assert scorecard.order_stability_rate == 1.0
    assert scorecard.description_robustness_rate == 1.0
    assert scorecard.noise_resistance_rate >= 0.95
