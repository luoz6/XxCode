import pytest

from tests.memory.helpers.extraction_eval import (
    ExtractionEvalCase,
    build_extraction_scorecard,
    classify_operations,
    compute_extraction_metrics,
    extraction_quality_cases,
    flatten_conversation_text,
    format_extraction_scorecard,
    memory_file,
)


def test_flatten_conversation_text_handles_string_and_text_blocks():
    conversation = [
        {"role": "user", "content": "I prefer snake_case in Python tests."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will remember snake_case."},
                {"type": "tool_use", "name": "write_file"},
            ],
        },
    ]

    text = flatten_conversation_text(conversation)

    assert "I prefer snake_case" in text
    assert "I will remember snake_case" in text
    assert "write_file" not in text


def test_classify_operations_reports_create_update_delete_and_noop():
    existing = {
        "keep.md": "same",
        "update.md": "old",
        "delete.md": "remove",
    }
    candidate = {
        "keep.md": "same",
        "update.md": "new",
        "create.md": "created",
    }

    operations = classify_operations(existing, candidate)

    assert operations.created_filenames == {"create.md"}
    assert operations.updated_filenames == {"update.md"}
    assert operations.deleted_filenames == {"delete.md"}
    assert operations.no_op_filenames == {"keep.md"}


def test_valid_candidate_memory_reports_validity_and_completeness():
    case = ExtractionEvalCase(
        case_id="valid-candidate",
        conversation=[
            {"role": "user", "content": "I prefer snake_case in Python tests."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "python-style.md": memory_file(
                "user",
                "Python Style",
                "User prefers snake_case in Python tests",
                "Use snake_case when writing Python tests for the user.",
            ),
        },
        expected_memory_filenames={"python-style.md"},
        expected_facts={"user prefers snake_case python tests"},
        expected_types={"python-style.md": "user"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.case_id == "valid-candidate"
    assert metrics.write_validity_rate == 1.0
    assert metrics.field_completeness_rate == 1.0
    assert metrics.memory_type_accuracy == 1.0
    assert metrics.invalid_candidate_filenames == set()
    assert metrics.operations.created_filenames == {"python-style.md"}


def test_incomplete_candidate_memory_reduces_field_completeness():
    case = ExtractionEvalCase(
        case_id="incomplete-candidate",
        conversation=[],
        existing_memory_files={},
        candidate_memory_files={
            "incomplete.md": memory_file(
                "user",
                "Incomplete",
                "",
                "",
            ),
        },
        expected_memory_filenames={"incomplete.md"},
        expected_facts=set(),
        expected_types={"incomplete.md": "user"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.write_validity_rate == 1.0
    assert metrics.field_completeness_rate == 0.0
    assert metrics.memory_type_accuracy == 1.0
    assert metrics.invalid_candidate_filenames == set()


def test_expected_fact_coverage_uses_lexical_token_matching():
    case = ExtractionEvalCase(
        case_id="fact-coverage",
        conversation=[
            {"role": "user", "content": "I prefer snake_case in Python tests."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "python-style.md": memory_file(
                "user",
                "Python Style",
                "User prefers snake_case",
                "Use snake_case in Python tests.",
            ),
        },
        expected_memory_filenames={"python-style.md"},
        expected_facts={
            "user prefers snake_case",
            "python tests",
            "pytest strict fixtures",
        },
        expected_types={"python-style.md": "user"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.expected_fact_coverage == 2 / 3
    assert metrics.missing_expected_facts == {"pytest strict fixtures"}


def test_grounding_rate_uses_claim_text_as_default_evidence():
    case = ExtractionEvalCase(
        case_id="grounding-default-evidence",
        conversation=[
            {"role": "user", "content": "Use pandas dataframes for analysis."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "analysis-style.md": memory_file(
                "user",
                "Analysis Style",
                "Use pandas dataframes",
                "Use pandas dataframes for analysis.",
            ),
        },
        expected_memory_filenames={"analysis-style.md"},
        expected_facts={"use pandas dataframes"},
        expected_types={"analysis-style.md": "user"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.grounding_rate == 1.0


def test_grounding_rate_uses_source_evidence_override():
    case = ExtractionEvalCase(
        case_id="grounding-override",
        conversation=[
            {"role": "user", "content": "Use pathlib instead of os.path."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "path-style.md": memory_file(
                "feedback",
                "Path Style",
                "Prefer pathlib APIs",
                "Prefer pathlib APIs for path manipulation.",
            ),
        },
        expected_memory_filenames={"path-style.md"},
        expected_facts={"prefer pathlib APIs"},
        expected_types={"path-style.md": "feedback"},
        source_evidence={"prefer pathlib APIs": {"pathlib instead of os path"}},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.grounding_rate == 1.0


def test_forbidden_fact_leak_reduces_noise_suppression_rate():
    case = ExtractionEvalCase(
        case_id="noise-leak",
        conversation=[
            {"role": "user", "content": "Temporarily debug port 5432 today."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "debug-note.md": memory_file(
                "project",
                "Debug Note",
                "Temporary port 5432 debug",
                "Temporarily debug port 5432 today.",
            ),
        },
        expected_memory_filenames=set(),
        expected_facts=set(),
        expected_types={"debug-note.md": "project"},
        forbidden_facts={"temporary port 5432 debug", "secret token abc"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.noise_suppression_rate == 0.5
    assert metrics.forbidden_fact_leak_count == 1
    assert metrics.leaked_forbidden_facts == {"temporary port 5432 debug"}


def test_empty_forbidden_facts_excludes_noise_suppression_rate():
    case = ExtractionEvalCase(
        case_id="no-forbidden-facts",
        conversation=[],
        existing_memory_files={},
        candidate_memory_files={},
        expected_memory_filenames=set(),
        expected_facts=set(),
        expected_types={},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.noise_suppression_rate is None
    assert metrics.forbidden_fact_leak_count == 0


def test_duplicate_control_checks_only_newly_created_files():
    existing = memory_file(
        "user",
        "Existing Style",
        "User prefers snake_case",
        "User prefers snake_case in Python tests.",
    )
    case = ExtractionEvalCase(
        case_id="duplicate-created",
        conversation=[
            {"role": "user", "content": "I prefer snake_case in Python tests."},
        ],
        existing_memory_files={"existing-style.md": existing},
        candidate_memory_files={
            "existing-style.md": existing,
            "duplicate-style.md": memory_file(
                "user",
                "Duplicate Style",
                "User prefers snake_case",
                "User prefers snake_case in Python tests.",
            ),
        },
        expected_memory_filenames={"existing-style.md"},
        expected_facts={"user prefers snake_case"},
        expected_types={"existing-style.md": "user"},
        duplicate_facts={"user prefers snake_case"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.duplicate_control_rate == 0.0


def test_duplicate_control_passes_when_duplicate_fact_is_not_in_created_file():
    existing = memory_file(
        "user",
        "Existing Style",
        "User prefers snake_case",
        "User prefers snake_case in Python tests.",
    )
    case = ExtractionEvalCase(
        case_id="duplicate-not-created",
        conversation=[
            {"role": "user", "content": "I prefer snake_case in Python tests."},
        ],
        existing_memory_files={"existing-style.md": existing},
        candidate_memory_files={"existing-style.md": existing},
        expected_memory_filenames={"existing-style.md"},
        expected_facts={"user prefers snake_case"},
        expected_types={"existing-style.md": "user"},
        duplicate_facts={"user prefers snake_case"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.duplicate_control_rate == 1.0


def test_conflict_update_correctness_requires_latest_and_no_obsolete_fact():
    case = ExtractionEvalCase(
        case_id="conflict-updated",
        conversation=[
            {"role": "user", "content": "Actually use pathlib instead of os.path."},
        ],
        existing_memory_files={
            "path-style.md": memory_file(
                "feedback",
                "Path Style",
                "Prefer os.path",
                "Prefer os.path for path manipulation.",
            ),
        },
        candidate_memory_files={
            "path-style.md": memory_file(
                "feedback",
                "Path Style",
                "Prefer pathlib",
                "Prefer pathlib for path manipulation.",
            ),
        },
        expected_memory_filenames={"path-style.md"},
        expected_facts={"prefer pathlib"},
        expected_types={"path-style.md": "feedback"},
        expected_latest_facts={"prefer pathlib"},
        obsolete_facts={"prefer os path"},
        expected_updated_filenames={"path-style.md"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.conflict_update_correctness == 1.0


def test_conflict_update_correctness_fails_when_obsolete_fact_remains():
    case = ExtractionEvalCase(
        case_id="conflict-obsolete-remains",
        conversation=[
            {"role": "user", "content": "Actually use pathlib instead of os.path."},
        ],
        existing_memory_files={
            "path-style.md": memory_file(
                "feedback",
                "Path Style",
                "Prefer os.path",
                "Prefer os.path for path manipulation.",
            ),
        },
        candidate_memory_files={
            "path-style.md": memory_file(
                "feedback",
                "Path Style",
                "Prefer pathlib but keep os.path",
                "Prefer pathlib now. Previously prefer os.path.",
            ),
        },
        expected_memory_filenames={"path-style.md"},
        expected_facts={"prefer pathlib"},
        expected_types={"path-style.md": "feedback"},
        expected_latest_facts={"prefer pathlib"},
        obsolete_facts={"prefer os path"},
        expected_updated_filenames={"path-style.md"},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.conflict_update_correctness == 0.0


def test_expected_updated_filename_must_be_classified_as_updated():
    unchanged = memory_file(
        "project",
        "Project Rule",
        "Always run tests",
        "Always run tests before completion.",
    )
    case = ExtractionEvalCase(
        case_id="missing-update",
        conversation=[],
        existing_memory_files={"project-rule.md": unchanged},
        candidate_memory_files={"project-rule.md": unchanged},
        expected_memory_filenames={"project-rule.md"},
        expected_facts=set(),
        expected_types={"project-rule.md": "project"},
        expected_updated_filenames={"project-rule.md"},
    )

    with pytest.raises(ValueError, match="expected updated files were not updated"):
        compute_extraction_metrics(case)


def test_expected_deleted_filename_must_be_classified_as_deleted():
    existing = memory_file(
        "project",
        "Temporary Note",
        "Temporary debug note",
        "Temporary debug note for today.",
    )
    case = ExtractionEvalCase(
        case_id="missing-delete",
        conversation=[],
        existing_memory_files={"temporary-note.md": existing},
        candidate_memory_files={"temporary-note.md": existing},
        expected_memory_filenames=set(),
        expected_facts=set(),
        expected_types={},
        expected_deleted_filenames={"temporary-note.md"},
    )

    with pytest.raises(ValueError, match="expected deleted files were not deleted"):
        compute_extraction_metrics(case)


def test_extraction_quality_cases_have_expected_positive_metrics():
    metrics_by_case = {
        case.case_id: compute_extraction_metrics(case)
        for case in extraction_quality_cases()
    }

    healthy = metrics_by_case["captures-user-style"]
    assert healthy.write_validity_rate == 1.0
    assert healthy.field_completeness_rate == 1.0
    assert healthy.expected_memory_coverage == 1.0
    assert healthy.expected_fact_coverage == 1.0
    assert healthy.grounding_rate == 1.0

    noisy = metrics_by_case["rejects-temporary-debug-noise"]
    assert noisy.noise_suppression_rate == 1.0
    assert noisy.forbidden_fact_leak_count == 0


def test_extraction_quality_risk_cases_expose_expected_weaknesses():
    metrics_by_case = {
        case.case_id: compute_extraction_metrics(case)
        for case in extraction_quality_cases()
    }

    assert metrics_by_case["wrong-type-risk"].memory_type_accuracy < 1.0
    assert metrics_by_case["missing-fact-risk"].expected_fact_coverage < 1.0
    assert metrics_by_case["ungrounded-risk"].grounding_rate < 1.0
    assert metrics_by_case["duplicate-risk"].duplicate_control_rate == 0.0
    assert metrics_by_case["conflict-risk"].conflict_update_correctness == 0.0


def test_extraction_scorecard_excludes_none_metrics_from_optional_means():
    cases = extraction_quality_cases()
    metrics = [compute_extraction_metrics(case) for case in cases]

    scorecard = build_extraction_scorecard(metrics)

    assert scorecard.n_cases == len(cases)
    assert scorecard.n_type_cases > 0
    assert scorecard.n_noise_cases > 0
    assert scorecard.n_duplicate_cases > 0
    assert scorecard.n_conflict_cases > 0
    assert 0.0 <= scorecard.mean_memory_type_accuracy <= 1.0
    assert 0.0 <= scorecard.mean_noise_suppression_rate <= 1.0
    assert 0.0 <= scorecard.mean_duplicate_control_rate <= 1.0
    assert 0.0 <= scorecard.mean_conflict_update_correctness <= 1.0


def test_extraction_scorecard_summary_includes_key_metrics():
    metrics = [
        compute_extraction_metrics(case)
        for case in extraction_quality_cases()
    ]
    scorecard = build_extraction_scorecard(metrics)

    summary = format_extraction_scorecard(scorecard)

    assert "n_cases=" in summary
    assert "mean_write_validity_rate=" in summary
    assert "n_noise_cases=" in summary
    assert "mean_noise_suppression_rate=" in summary
