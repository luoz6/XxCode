from __future__ import annotations

import re
from dataclasses import dataclass, field


_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class EffectivenessEvalCase:
    case_id: str
    query: str
    recalled_memories: dict[str, str]
    answer: str | None = None
    baseline_answer: str | None = None
    expected_answer_facts: set[str] = field(default_factory=set)
    expected_memory_facts_used: set[str] = field(default_factory=set)
    forbidden_answer_facts: set[str] = field(default_factory=set)
    expected_preferences_applied: set[str] = field(default_factory=set)
    obsolete_facts: set[str] = field(default_factory=set)
    risk_labels: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class EffectivenessMetrics:
    case_id: str
    answer: str
    answer_fact_coverage: float
    memory_fact_usage_rate: float | None
    preference_adherence_rate: float | None
    forbidden_fact_absence_rate: float | None
    obsolete_fact_suppression_rate: float | None
    memory_lift: float | None
    memory_lift_delta: float | None
    missing_answer_facts: set[str]
    used_memory_facts: set[str]
    leaked_forbidden_facts: set[str]
    leaked_obsolete_facts: set[str]


@dataclass(frozen=True)
class EffectivenessScorecard:
    n_cases: int = 0
    mean_answer_fact_coverage: float = 0.0
    n_memory_usage_cases: int = 0
    mean_memory_fact_usage_rate: float = 0.0
    n_preference_cases: int = 0
    mean_preference_adherence_rate: float = 0.0
    n_forbidden_cases: int = 0
    mean_forbidden_fact_absence_rate: float = 0.0
    n_obsolete_cases: int = 0
    mean_obsolete_fact_suppression_rate: float = 0.0
    n_lift_cases: int = 0
    memory_lift_rate: float = 0.0
    mean_memory_lift_delta: float = 0.0


def represented_facts(facts: set[str], texts: list[str]) -> set[str]:
    represented: set[str] = set()
    text_token_sets = [_tokens(text) for text in texts]
    for fact in facts:
        fact_tokens = _tokens(fact)
        if fact_tokens and any(
            fact_tokens.issubset(text_tokens)
            for text_tokens in text_token_sets
        ):
            represented.add(fact)
        elif not fact_tokens and not fact.strip():
            represented.add(fact)
    return represented


def compute_effectiveness_metrics(case: EffectivenessEvalCase) -> EffectivenessMetrics:
    _validate_case(case)
    answer = _select_answer(case)
    answer_texts = [answer]

    represented_answer_facts = represented_facts(
        case.expected_answer_facts,
        answer_texts,
    )
    used_memory_facts = represented_facts(
        case.expected_memory_facts_used,
        answer_texts,
    )
    represented_preferences = represented_facts(
        case.expected_preferences_applied,
        answer_texts,
    )
    leaked_forbidden = represented_facts(
        case.forbidden_answer_facts,
        answer_texts,
    )
    leaked_obsolete = represented_facts(case.obsolete_facts, answer_texts)

    memory_lift = None
    memory_lift_delta = None
    if case.baseline_answer is not None:
        baseline_coverage = _coverage(case.expected_answer_facts, case.baseline_answer)
        answer_coverage = _coverage(case.expected_answer_facts, answer)
        memory_lift_delta = answer_coverage - baseline_coverage
        memory_lift = 1.0 if memory_lift_delta > 0 else 0.0

    return EffectivenessMetrics(
        case_id=case.case_id,
        answer=answer,
        answer_fact_coverage=_safe_div(
            len(represented_answer_facts),
            len(case.expected_answer_facts),
            1.0,
        ),
        memory_fact_usage_rate=_optional_rate(
            len(used_memory_facts),
            len(case.expected_memory_facts_used),
        ),
        preference_adherence_rate=_optional_rate(
            len(represented_preferences),
            len(case.expected_preferences_applied),
        ),
        forbidden_fact_absence_rate=_optional_rate(
            len(case.forbidden_answer_facts) - len(leaked_forbidden),
            len(case.forbidden_answer_facts),
        ),
        obsolete_fact_suppression_rate=_optional_rate(
            len(case.obsolete_facts) - len(leaked_obsolete),
            len(case.obsolete_facts),
        ),
        memory_lift=memory_lift,
        memory_lift_delta=memory_lift_delta,
        missing_answer_facts=case.expected_answer_facts - represented_answer_facts,
        used_memory_facts=used_memory_facts,
        leaked_forbidden_facts=leaked_forbidden,
        leaked_obsolete_facts=leaked_obsolete,
    )


def effectiveness_benchmark_cases() -> list[EffectivenessEvalCase]:
    return [
        EffectivenessEvalCase(
            case_id="preference-adherence",
            query="How should I write Python tests?",
            recalled_memories={
                "style.md": "User prefers snake_case in Python tests.",
            },
            expected_answer_facts={"user prefers snake_case", "python tests"},
            expected_memory_facts_used={"user prefers snake_case"},
            expected_preferences_applied={"user prefers snake_case"},
        ),
        EffectivenessEvalCase(
            case_id="project-rule-usage",
            query="What should I do before completing the task?",
            recalled_memories={
                "project-rule.md": "Always run pytest before completion.",
            },
            expected_answer_facts={"run pytest", "before completion"},
            expected_memory_facts_used={"run pytest"},
        ),
        EffectivenessEvalCase(
            case_id="stale-memory-risk",
            query="Which path API should I use?",
            recalled_memories={
                "path-style.md": "Use pathlib instead of os.path.",
            },
            answer="Use os.path for path manipulation.",
            expected_answer_facts={"use pathlib"},
            expected_memory_facts_used={"use pathlib"},
            obsolete_facts={"use os path"},
            risk_labels={"stale-memory"},
        ),
        EffectivenessEvalCase(
            case_id="memory-lift",
            query="How should I write tests?",
            recalled_memories={
                "style.md": "User prefers snake_case in Python tests.",
            },
            baseline_answer="Write clear tests.",
            expected_answer_facts={"user prefers snake_case", "python tests"},
            expected_memory_facts_used={"user prefers snake_case"},
            expected_preferences_applied={"user prefers snake_case"},
        ),
        EffectivenessEvalCase(
            case_id="project-lift",
            query="What should I do before completion?",
            recalled_memories={
                "project-rule.md": "Always run pytest before completion.",
            },
            baseline_answer="Review your work before completion.",
            expected_answer_facts={"run pytest", "before completion"},
            expected_memory_facts_used={"run pytest"},
        ),
        EffectivenessEvalCase(
            case_id="generic-answer-risk",
            query="How should I write tests?",
            recalled_memories={
                "style.md": "User prefers snake_case in Python tests.",
            },
            answer="Write clear tests.",
            expected_answer_facts={"user prefers snake_case"},
            expected_memory_facts_used={"user prefers snake_case"},
            expected_preferences_applied={"user prefers snake_case"},
            risk_labels={"generic-answer"},
        ),
        EffectivenessEvalCase(
            case_id="ungrounded-answer-risk",
            query="How should I write tests?",
            recalled_memories={
                "style.md": "User prefers snake_case in Python tests.",
            },
            answer="Use snake_case and secret token abc.",
            expected_answer_facts={"user prefers snake_case"},
            expected_memory_facts_used={"user prefers snake_case"},
            expected_preferences_applied={"user prefers snake_case"},
            forbidden_answer_facts={"secret token abc"},
            risk_labels={"ungrounded-answer"},
        ),
    ]


def build_effectiveness_scorecard(
    metrics: list[EffectivenessMetrics],
) -> EffectivenessScorecard:
    if not metrics:
        return EffectivenessScorecard()
    memory_values = [
        metric.memory_fact_usage_rate
        for metric in metrics
        if metric.memory_fact_usage_rate is not None
    ]
    preference_values = [
        metric.preference_adherence_rate
        for metric in metrics
        if metric.preference_adherence_rate is not None
    ]
    forbidden_values = [
        metric.forbidden_fact_absence_rate
        for metric in metrics
        if metric.forbidden_fact_absence_rate is not None
    ]
    obsolete_values = [
        metric.obsolete_fact_suppression_rate
        for metric in metrics
        if metric.obsolete_fact_suppression_rate is not None
    ]
    lift_values = [
        metric.memory_lift
        for metric in metrics
        if metric.memory_lift is not None
    ]
    lift_delta_values = [
        metric.memory_lift_delta
        for metric in metrics
        if metric.memory_lift_delta is not None
    ]
    return EffectivenessScorecard(
        n_cases=len(metrics),
        mean_answer_fact_coverage=(
            sum(metric.answer_fact_coverage for metric in metrics) / len(metrics)
        ),
        n_memory_usage_cases=len(memory_values),
        mean_memory_fact_usage_rate=_mean_optional(memory_values),
        n_preference_cases=len(preference_values),
        mean_preference_adherence_rate=_mean_optional(preference_values),
        n_forbidden_cases=len(forbidden_values),
        mean_forbidden_fact_absence_rate=_mean_optional(forbidden_values),
        n_obsolete_cases=len(obsolete_values),
        mean_obsolete_fact_suppression_rate=_mean_optional(obsolete_values),
        n_lift_cases=len(lift_values),
        memory_lift_rate=_mean_optional(lift_values),
        mean_memory_lift_delta=_mean_optional(lift_delta_values),
    )


def format_effectiveness_scorecard(scorecard: EffectivenessScorecard) -> str:
    return (
        "effectiveness "
        f"n_cases={scorecard.n_cases} "
        f"mean_answer_fact_coverage={scorecard.mean_answer_fact_coverage:.3f} "
        f"n_memory_usage_cases={scorecard.n_memory_usage_cases} "
        f"mean_memory_fact_usage_rate={scorecard.mean_memory_fact_usage_rate:.3f} "
        f"n_preference_cases={scorecard.n_preference_cases} "
        f"mean_preference_adherence_rate={scorecard.mean_preference_adherence_rate:.3f} "
        f"n_forbidden_cases={scorecard.n_forbidden_cases} "
        f"mean_forbidden_fact_absence_rate={scorecard.mean_forbidden_fact_absence_rate:.3f} "
        f"n_obsolete_cases={scorecard.n_obsolete_cases} "
        f"mean_obsolete_fact_suppression_rate={scorecard.mean_obsolete_fact_suppression_rate:.3f} "
        f"n_lift_cases={scorecard.n_lift_cases} "
        f"memory_lift_rate={scorecard.memory_lift_rate:.3f} "
        f"mean_memory_lift_delta={scorecard.mean_memory_lift_delta:.3f}"
    )


def _validate_case(case: EffectivenessEvalCase) -> None:
    memory_texts = list(case.recalled_memories.values())
    missing_memory_facts = (
        case.expected_memory_facts_used
        - represented_facts(case.expected_memory_facts_used, memory_texts)
    )
    if missing_memory_facts:
        raise ValueError(
            f"{case.case_id}: expected_memory_facts_used missing from "
            f"recalled_memories: {sorted(missing_memory_facts)}"
        )

    query_tokens = _tokens(case.query)
    query_sourced_memory_facts = {
        fact
        for fact in case.expected_memory_facts_used
        if _tokens(fact).issubset(query_tokens)
    }
    if query_sourced_memory_facts:
        raise ValueError(
            f"{case.case_id}: expected_memory_facts_used already present in "
            f"query: {sorted(query_sourced_memory_facts)}"
        )

    preferences_not_answer_facts = (
        case.expected_preferences_applied - case.expected_answer_facts
    )
    if preferences_not_answer_facts:
        raise ValueError(
            f"{case.case_id}: expected_preferences_applied must be included "
            f"in expected_answer_facts: {sorted(preferences_not_answer_facts)}"
        )

    forbidden_overlap = case.forbidden_answer_facts & case.expected_answer_facts
    if forbidden_overlap:
        raise ValueError(
            f"{case.case_id}: forbidden_answer_facts overlap "
            f"expected_answer_facts: {sorted(forbidden_overlap)}"
        )

    obsolete_overlap = case.obsolete_facts & case.expected_answer_facts
    if obsolete_overlap:
        raise ValueError(
            f"{case.case_id}: obsolete_facts overlap expected_answer_facts: "
            f"{sorted(obsolete_overlap)}"
        )

    if case.baseline_answer is not None and not case.baseline_answer.strip():
        raise ValueError(f"{case.case_id}: baseline_answer must be non-empty")
    if case.answer is not None and not case.answer.strip():
        raise ValueError(f"{case.case_id}: answer must be non-empty")
    if not case.expected_answer_facts and not case.risk_labels:
        raise ValueError(
            f"{case.case_id}: empty expected_answer_facts requires risk_labels"
        )


def _select_answer(case: EffectivenessEvalCase) -> str:
    if case.answer is not None:
        return case.answer
    return _deterministic_assistant(case.query, case.recalled_memories)


def _deterministic_assistant(query: str, recalled_memories: dict[str, str]) -> str:
    query_tokens = _tokens(query)
    ranked: list[tuple[int, str, str]] = []
    for filename, memory_text in recalled_memories.items():
        memory_tokens = _tokens(memory_text)
        # Phase one uses single-token lexical overlap. False positives from
        # common words are a known limitation controlled by the curated corpus.
        score = len(query_tokens & memory_tokens)
        if score > 0:
            ranked.append((-score, filename, memory_text.strip()))
    ranked.sort(key=lambda item: (item[0], item[1]))
    selected = [memory_text for _score, _filename, memory_text in ranked]
    if not selected:
        return f"Answer based on the query: {query}"
    return " ".join(selected)


def _coverage(facts: set[str], text: str) -> float:
    return _safe_div(len(represented_facts(facts, [text])), len(facts), 1.0)


def _optional_rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _safe_div(numerator: int, denominator: int, empty_value: float) -> float:
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _mean_optional(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
