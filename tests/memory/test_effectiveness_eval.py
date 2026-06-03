import pytest

from tests.memory.helpers.effectiveness_eval import (
    EffectivenessEvalCase,
    compute_effectiveness_metrics,
    represented_facts,
)


def test_represented_facts_uses_token_subset_matching():
    facts = {"use snake_case", "prefer pytest fixtures", "missing fact"}
    texts = [
        "The answer says to use snake_case in Python tests.",
        "It also says prefer pytest fixtures for setup.",
    ]

    assert represented_facts(facts, texts) == {
        "use snake_case",
        "prefer pytest fixtures",
    }


def test_validate_case_rejects_memory_fact_already_in_query():
    case = EffectivenessEvalCase(
        case_id="memory-fact-in-query",
        query="Please use snake_case for tests.",
        recalled_memories={
            "style.md": "Use snake_case in Python tests.",
        },
        answer="Use snake_case for tests.",
        expected_answer_facts={"use snake_case"},
        expected_memory_facts_used={"use snake_case"},
    )

    with pytest.raises(ValueError, match="already present in query"):
        compute_effectiveness_metrics(case)


def test_validate_case_rejects_memory_fact_missing_from_recalled_memory():
    case = EffectivenessEvalCase(
        case_id="memory-fact-missing",
        query="How should I write tests?",
        recalled_memories={
            "style.md": "User prefers concise answers.",
        },
        answer="Use snake_case for tests.",
        expected_answer_facts={"use snake_case"},
        expected_memory_facts_used={"use snake_case"},
    )

    with pytest.raises(ValueError, match="missing from recalled_memories"):
        compute_effectiveness_metrics(case)


def test_validate_case_rejects_preference_not_in_expected_answer_facts():
    case = EffectivenessEvalCase(
        case_id="preference-not-answer-fact",
        query="How should I write tests?",
        recalled_memories={
            "style.md": "User prefers snake_case in Python tests.",
        },
        answer="Use snake_case in Python tests.",
        expected_answer_facts={"python tests"},
        expected_preferences_applied={"use snake_case"},
    )

    with pytest.raises(ValueError, match="expected_preferences_applied"):
        compute_effectiveness_metrics(case)


def test_compute_metrics_scores_answer_facts_memory_usage_and_absence():
    case = EffectivenessEvalCase(
        case_id="core-metrics",
        query="How should I write Python tests?",
        recalled_memories={
            "style.md": "Use snake_case in Python tests.",
            "obsolete.md": "Previously preferred camelCase in tests.",
        },
        answer="Use snake_case in Python tests.",
        expected_answer_facts={"use snake_case", "python tests"},
        expected_memory_facts_used={"use snake_case"},
        expected_preferences_applied={"use snake_case"},
        forbidden_answer_facts={"secret token abc"},
        obsolete_facts={"camelCase tests"},
    )

    metrics = compute_effectiveness_metrics(case)

    assert metrics.case_id == "core-metrics"
    assert metrics.answer == "Use snake_case in Python tests."
    assert metrics.answer_fact_coverage == 1.0
    assert metrics.memory_fact_usage_rate == 1.0
    assert metrics.preference_adherence_rate == 1.0
    assert metrics.forbidden_fact_absence_rate == 1.0
    assert metrics.obsolete_fact_suppression_rate == 1.0
    assert metrics.memory_lift is None
    assert metrics.memory_lift_delta is None
    assert metrics.missing_answer_facts == set()
    assert metrics.used_memory_facts == {"use snake_case"}
    assert metrics.leaked_forbidden_facts == set()
    assert metrics.leaked_obsolete_facts == set()
