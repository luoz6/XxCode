import pytest

from tests.memory.helpers.recall_eval import (
    DeterministicRecallClient,
    QualityMetrics,
    RecallEvalCase,
    build_quality_scorecard,
    compute_quality_metrics,
    format_quality_scorecard,
    quality_benchmark_cases,
    run_recall_case,
    validate_case,
)


def _make_recall_case(
    *,
    case_id: str,
    query: str = "remember pandas preferences",
    index_content: str = "",
    memory_files: dict[str, str] | None = None,
    expected_filenames: set[str] | None = None,
    expected_top1: str | None = None,
) -> RecallEvalCase:
    return RecallEvalCase(
        case_id=case_id,
        query=query,
        index_content=index_content,
        memory_files=memory_files or {},
        expected_filenames=expected_filenames or set(),
        expected_top1=expected_top1,
    )


async def _complete_selector(user_content: str) -> str:
    client = DeterministicRecallClient()
    return await client.complete(
        system_prompt="selector",
        messages=[{"role": "user", "content": user_content}],
        max_tokens=256,
    )


def test_validate_case_rejects_index_entry_without_memory_file():
    case = _make_recall_case(
        case_id="ghost-index-entry",
        index_content="- [Ghost](ghost.md) - User prefers pandas\n",
        expected_filenames={"ghost.md"},
        expected_top1="ghost.md",
    )

    with pytest.raises(ValueError, match="ghost.md"):
        validate_case(case)


def test_validate_case_rejects_expected_file_missing_from_index():
    case = _make_recall_case(
        case_id="expected-not-indexed",
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


@pytest.mark.asyncio
async def test_deterministic_selector_reads_available_memories_section():
    response = await _complete_selector(
        "Query: pandas dataframe analysis\n\n"
        "Available memories:\n"
        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
        "- [indexed] release-plan.md: Release deadline planning\n"
    )

    assert response == '["pandas-style.md"]'


@pytest.mark.asyncio
async def test_deterministic_selector_returns_empty_when_no_terms_overlap():
    response = await _complete_selector(
        "Query: image rendering canvas\n\n"
        "Available memories:\n"
        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
        "- [indexed] release-plan.md: Release deadline planning\n"
    )

    assert response == "[]"


@pytest.mark.asyncio
async def test_deterministic_selector_fails_when_manifest_section_missing():
    with pytest.raises(ValueError, match="Available memories"):
        await _complete_selector("Query: pandas")


def test_quality_metrics_compute_precision_recall_f1_and_top1():
    case = _make_recall_case(
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


def test_quality_scorecard_excludes_cases_without_top1_expectation():
    no_top1_case = _make_recall_case(
        case_id="no-top1",
        query="pandas dataframe analysis",
        index_content="- [Pandas Style](pandas-style.md) - User prefers pandas\n",
        memory_files={
            "pandas-style.md": "---\nmetadata:\n  type: user\n---\n\nPandas",
        },
        expected_filenames={"pandas-style.md"},
    )
    with_top1_case = _make_recall_case(
        case_id="with-top1",
        query="release planning",
        index_content="- [Release Plan](release-plan.md) - Release planning\n",
        memory_files={
            "release-plan.md": "---\nmetadata:\n  type: project\n---\n\nRelease",
        },
        expected_filenames={"release-plan.md"},
        expected_top1="release-plan.md",
    )

    no_top1_metrics = compute_quality_metrics(
        no_top1_case,
        selected_filenames=["pandas-style.md"],
    )
    with_top1_metrics = compute_quality_metrics(
        with_top1_case,
        selected_filenames=["not-release-plan.md"],
    )

    scorecard = build_quality_scorecard([no_top1_metrics, with_top1_metrics])

    assert no_top1_metrics.top1_hit is None
    assert scorecard.n_cases == 2
    assert scorecard.n_top1_cases == 1
    assert scorecard.top1_hit_rate == 0.0


def test_quality_scorecard_summary_includes_case_count_and_key_metrics():
    scorecard = build_quality_scorecard([
        QualityMetrics(
            case_id="demo",
            selected_filenames=["a.md"],
            expected_filenames={"a.md"},
            precision_at_k=1.0,
            recall_at_k=1.0,
            f1_at_k=1.0,
            top1_hit=1.0,
            topk_full_match=1.0,
        )
    ])

    summary = format_quality_scorecard(scorecard)

    assert "n_cases=1" in summary
    assert "n_top1_cases=1" in summary
    assert "mean_f1_at_k=1.000" in summary
    assert "full_match_rate=1.000" in summary


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
    print(format_quality_scorecard(scorecard))
    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.mean_f1_at_k >= 0.95
    assert scorecard.top1_hit_rate >= 0.95
    assert scorecard.full_match_rate >= 0.95
