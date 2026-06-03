from tests.memory.helpers.index_eval import (
    GeneratedIndexEvalCase,
    RawIndexEvalCase,
    build_index_scorecard,
    compute_index_metrics,
    compute_generated_index_metrics,
    format_index_scorecard,
    generated_index_cases,
    raw_index_risk_cases,
    scan_raw_index_links,
)
from xxcode.memory.index import MAX_ENTRYPOINT_BYTES


def test_raw_link_scanner_keeps_duplicates_and_memory_md_references():
    links = scan_raw_index_links(
        "- [Index](MEMORY.md) - should be flagged\n"
        "- [Alpha](alpha.md) - first\n"
        "- [Alpha Again](alpha.md) - duplicate\n"
        "- [Ghost](ghost.md) - stale\n"
    )

    assert [link.filename for link in links] == [
        "MEMORY.md",
        "alpha.md",
        "alpha.md",
        "ghost.md",
    ]
    assert links[0].title == "Index"
    assert links[2].description == "duplicate"


def test_structural_metrics_detect_stale_duplicate_and_memory_md_reference():
    case = RawIndexEvalCase(
        case_id="raw-structural-risk",
        index_content=(
            "- [Index](MEMORY.md) - should be flagged\n"
            "- [Alpha](alpha.md) - first\n"
            "- [Alpha Again](alpha.md) - duplicate\n"
            "- [Ghost](ghost.md) - stale\n"
        ),
        memory_files={
            "alpha.md": _memory_file("user", "Alpha", "First alpha detail"),
        },
        expected_present_filenames={"alpha.md"},
        risk_labels={"memory-md", "duplicate", "stale"},
    )

    metrics = compute_index_metrics(
        case.index_content,
        case.memory_files,
        case_id=case.case_id,
    )

    assert metrics.case_id == "raw-structural-risk"
    assert metrics.indexed_file_count == 2
    assert metrics.memory_file_count == 1
    assert metrics.coverage_rate == 1.0
    assert metrics.stale_reference_rate == 1 / 4
    assert metrics.duplicate_reference_rate == 1 / 4
    assert metrics.parseable_line_rate == 1.0
    assert metrics.memory_md_exclusion == 0.0


def test_generated_index_case_reports_healthy_structure(tmp_path):
    case = GeneratedIndexEvalCase(
        case_id="healthy-generated",
        memory_files={
            "user-style.md": _memory_file("user", "User Style", "User prefers pytest"),
            "project-plan.md": _memory_file(
                "project",
                "Project Plan",
                "Project release plan",
            ),
            "feedback-rule.md": _memory_file(
                "feedback",
                "Feedback Rule",
                "Always run tests",
            ),
            "reference-doc.md": _memory_file(
                "reference",
                "Reference Doc",
                "External docs",
            ),
        },
        expected_indexed_filenames={
            "user-style.md",
            "project-plan.md",
            "feedback-rule.md",
            "reference-doc.md",
        },
        expected_type_order=["user", "project", "feedback", "reference"],
    )

    metrics = compute_generated_index_metrics(case, tmp_path / case.case_id)

    assert metrics.coverage_rate == 1.0
    assert metrics.stale_reference_rate == 0.0
    assert metrics.duplicate_reference_rate == 0.0
    assert metrics.parseable_line_rate == 1.0
    assert metrics.memory_md_exclusion == 1.0
    assert metrics.type_order_adherence == 1.0
    assert metrics.was_line_truncated is False
    assert metrics.was_byte_truncated is False


def test_generated_index_scorecard_reports_case_count(tmp_path):
    metrics = [
        compute_generated_index_metrics(case, tmp_path / case.case_id)
        for case in generated_index_cases()
    ]

    scorecard = build_index_scorecard(metrics)

    print(format_index_scorecard(scorecard))

    assert scorecard.n_cases == len(generated_index_cases())
    assert scorecard.mean_coverage_rate == 1.0
    assert scorecard.mean_stale_reference_rate == 0.0
    assert scorecard.type_order_adherence_rate == 1.0


def test_description_metrics_detect_generic_and_non_discriminative_entries():
    case = RawIndexEvalCase(
        case_id="description-risk",
        index_content=(
            "- [Todo](todo.md) - todo\n"
            "- [Project Plan](project-plan.md) - project plan\n"
            "- [Pandas Style](pandas-style.md) - User prefers pandas dataframes\n"
        ),
        memory_files={
            "todo.md": _memory_file("reference", "Todo", "todo"),
            "project-plan.md": _memory_file("project", "Project Plan", "project plan"),
            "pandas-style.md": _memory_file(
                "user",
                "Pandas Style",
                "User prefers pandas dataframes",
            ),
        },
        expected_present_filenames={
            "todo.md",
            "project-plan.md",
            "pandas-style.md",
        },
        risk_labels={"generic-description", "non-discriminative"},
    )

    metrics = compute_index_metrics(
        case.index_content,
        case.memory_files,
        case_id=case.case_id,
    )

    assert metrics.description_present_rate == 1.0
    assert metrics.generic_description_rate == 1 / 3
    assert metrics.discriminative_token_rate == 1 / 3


def test_raw_risk_cases_detect_their_expected_risks():
    metrics_by_case = {
        case.case_id: compute_index_metrics(
            case.index_content,
            case.memory_files,
            case_id=case.case_id,
        )
        for case in raw_index_risk_cases()
    }

    assert metrics_by_case["raw-stale-reference"].stale_reference_rate > 0.0
    assert metrics_by_case["raw-duplicate-reference"].duplicate_reference_rate > 0.0
    assert metrics_by_case["raw-generic-description"].generic_description_rate > 0.0


def test_budget_metrics_detect_truncation_risk():
    long_line = "- [Huge](huge.md) - " + ("x" * MAX_ENTRYPOINT_BYTES)
    metrics = compute_index_metrics(
        long_line + "\n",
        {"huge.md": _memory_file("reference", "Huge", "Huge memory")},
        case_id="budget-risk",
    )

    assert metrics.byte_utilization > 1.0
    assert metrics.was_byte_truncated is True


def test_index_scorecard_summary_includes_key_metrics(tmp_path):
    metrics = [
        compute_generated_index_metrics(case, tmp_path / case.case_id)
        for case in generated_index_cases()
    ]
    scorecard = build_index_scorecard(metrics)

    summary = format_index_scorecard(scorecard)

    assert "n_cases=" in summary
    assert "mean_coverage_rate=1.000" in summary
    assert "truncated_case_count=0" in summary


def _memory_file(memory_type: str, name: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{description}\n"
    )
