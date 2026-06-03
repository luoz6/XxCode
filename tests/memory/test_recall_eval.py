import asyncio

import pytest

from tests.memory.helpers.recall_eval import (
    DeterministicRecallClient,
    QualityMetrics,
    RecallEvalCase,
    build_quality_scorecard,
    compute_quality_metrics,
    quality_benchmark_cases,
    run_recall_case,
    validate_case,
)


def test_validate_case_rejects_index_entry_without_memory_file():
    case = RecallEvalCase(
        case_id="ghost-index-entry",
        query="remember pandas preferences",
        index_content="- [Ghost](ghost.md) - User prefers pandas\n",
        memory_files={},
        expected_filenames={"ghost.md"},
        expected_top1="ghost.md",
    )

    with pytest.raises(ValueError, match="ghost.md"):
        validate_case(case)


def test_validate_case_rejects_expected_file_missing_from_index():
    case = RecallEvalCase(
        case_id="expected-not-indexed",
        query="remember pandas preferences",
        index_content="- [Other](other.md) - unrelated\n",
        memory_files={
            "other.md": "---\nmetadata:\n  type: user\n---\n\nOther",
            "pandas.md": "---\nmetadata:\n  type: user\n---\n\nPandas",
        },
        expected_filenames={"pandas.md"},
        expected_top1="pandas.md",
    )

    with pytest.raises(ValueError, match="pandas.md"):
        validate_case(case)


def test_deterministic_selector_reads_available_memories_section():
    async def _run():
        client = DeterministicRecallClient()
        response = await client.complete(
            system_prompt="selector",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Query: pandas dataframe analysis\n\n"
                        "Available memories:\n"
                        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
                        "- [indexed] release-plan.md: Release deadline planning\n"
                    ),
                }
            ],
            max_tokens=256,
        )

        assert response == '["pandas-style.md"]'

    asyncio.run(_run())


def test_deterministic_selector_fails_when_manifest_section_missing():
    async def _run():
        client = DeterministicRecallClient()
        with pytest.raises(ValueError, match="Available memories"):
            await client.complete(
                system_prompt="selector",
                messages=[{"role": "user", "content": "Query: pandas"}],
                max_tokens=256,
            )

    asyncio.run(_run())


def test_quality_metrics_compute_precision_recall_f1_and_top1():
    case = RecallEvalCase(
        case_id="metric-demo",
        query="pandas dataframe analysis",
        index_content=(
            "- [Pandas Style](pandas-style.md) - User prefers pandas dataframes\n"
            "- [Release Plan](release-plan.md) - Release deadline planning\n"
        ),
        memory_files={
            "pandas-style.md": "---\nmetadata:\n  type: user\n---\n\nPandas",
            "release-plan.md": "---\nmetadata:\n  type: project\n---\n\nRelease",
        },
        expected_filenames={"pandas-style.md"},
        expected_top1="pandas-style.md",
    )

    metrics = compute_quality_metrics(
        case,
        selected_filenames=["pandas-style.md", "release-plan.md"],
    )

    assert metrics == QualityMetrics(
        case_id="metric-demo",
        selected_filenames=["pandas-style.md", "release-plan.md"],
        expected_filenames={"pandas-style.md"},
        precision_at_k=0.5,
        recall_at_k=1.0,
        f1_at_k=2 / 3,
        top1_hit=1.0,
        topk_full_match=0.0,
    )


@pytest.mark.asyncio
async def test_quality_benchmark_cases_recall_expected_memories(tmp_path):
    results = []
    for case in quality_benchmark_cases():
        selected = await run_recall_case(case, tmp_path / case.case_id)
        metrics = compute_quality_metrics(case, selected)
        results.append(metrics)
        assert set(selected) == case.expected_filenames, (
            case.case_id,
            selected,
            case.expected_filenames,
        )
        if case.expected_top1 is not None:
            assert selected[0] == case.expected_top1

    scorecard = build_quality_scorecard(results)
    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.mean_f1_at_k >= 0.95
    assert scorecard.top1_hit_rate >= 0.95
    assert scorecard.full_match_rate >= 0.95
