import pytest

from tests.memory.helpers.extraction_eval import (
    ExtractionEvalCase,
    classify_operations,
    compute_extraction_metrics,
    flatten_conversation_text,
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
