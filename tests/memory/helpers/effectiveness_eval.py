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
    del recalled_memories
    return query


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


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
