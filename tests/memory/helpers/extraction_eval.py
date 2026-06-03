from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xxcode.memory.models import (
    MemoryEntry,
    MemoryType,
    parse_memory_file,
    serialize_memory_file,
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_VALID_MEMORY_TYPES = {memory_type.value for memory_type in MemoryType}


@dataclass(frozen=True)
class ExtractionEvalCase:
    case_id: str
    conversation: list[dict[str, Any]]
    existing_memory_files: dict[str, str]
    candidate_memory_files: dict[str, str]
    expected_memory_filenames: set[str]
    expected_facts: set[str]
    expected_types: dict[str, str]
    candidate_claims: set[str] = field(default_factory=set)
    source_evidence: dict[str, set[str]] = field(default_factory=dict)
    forbidden_facts: set[str] = field(default_factory=set)
    duplicate_facts: set[str] = field(default_factory=set)
    expected_latest_facts: set[str] = field(default_factory=set)
    obsolete_facts: set[str] = field(default_factory=set)
    expected_updated_filenames: set[str] = field(default_factory=set)
    expected_deleted_filenames: set[str] = field(default_factory=set)
    risk_labels: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class OperationClassification:
    created_filenames: set[str]
    updated_filenames: set[str]
    deleted_filenames: set[str]
    no_op_filenames: set[str]


@dataclass(frozen=True)
class ExtractionMetrics:
    case_id: str
    write_validity_rate: float
    field_completeness_rate: float
    memory_type_accuracy: float | None
    expected_memory_coverage: float
    expected_fact_coverage: float
    grounding_rate: float
    noise_suppression_rate: float | None
    forbidden_fact_leak_count: int
    duplicate_control_rate: float | None
    conflict_update_correctness: float | None
    invalid_candidate_filenames: set[str]
    missing_expected_facts: set[str]
    leaked_forbidden_facts: set[str]
    wrong_type_filenames: set[str]
    operations: OperationClassification


def flatten_conversation_text(conversation: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in conversation:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
    return "\n".join(parts)


def classify_operations(
    existing_memory_files: dict[str, str],
    candidate_memory_files: dict[str, str],
) -> OperationClassification:
    existing = set(existing_memory_files)
    candidate = set(candidate_memory_files)
    shared = existing & candidate
    return OperationClassification(
        created_filenames=candidate - existing,
        updated_filenames={
            filename
            for filename in shared
            if existing_memory_files[filename] != candidate_memory_files[filename]
        },
        deleted_filenames=existing - candidate,
        no_op_filenames={
            filename
            for filename in shared
            if existing_memory_files[filename] == candidate_memory_files[filename]
        },
    )


def compute_extraction_metrics(case: ExtractionEvalCase) -> ExtractionMetrics:
    _validate_case(case)
    operations = classify_operations(
        case.existing_memory_files,
        case.candidate_memory_files,
    )
    _validate_candidate_expectations(case, operations)
    parsed, invalid_filenames = _parse_candidate_files(case)
    parsed_existing, _existing_invalid_filenames = _parse_memory_files(
        case.existing_memory_files,
    )
    parsed_for_type_checks = {**parsed_existing, **parsed}
    candidate_text_by_file = {
        filename: _entry_text(entry) for filename, entry in parsed.items()
    }
    represented_expected_facts = _represented_facts(
        case.expected_facts,
        list(candidate_text_by_file.values()),
    )
    missing_expected_facts = case.expected_facts - represented_expected_facts
    grounding_rate = _grounding_rate(case)
    leaked_forbidden_facts = _represented_facts(
        case.forbidden_facts,
        list(candidate_text_by_file.values()),
    )
    noise_suppression_rate = None
    if case.forbidden_facts:
        noise_suppression_rate = _safe_div(
            len(case.forbidden_facts) - len(leaked_forbidden_facts),
            len(case.forbidden_facts),
            0.0,
        )
    duplicate_control_rate = _duplicate_control_rate(
        case,
        operations,
        candidate_text_by_file,
    )
    conflict_update_correctness = _conflict_update_correctness(
        case,
        list(candidate_text_by_file.values()),
    )

    valid_count = len(parsed)
    candidate_count = len(case.candidate_memory_files)
    write_validity_rate = _safe_div(valid_count, candidate_count, 1.0)
    field_completeness_rate = (
        _safe_div(
            sum(1 for entry in parsed.values() if _is_complete(entry)),
            valid_count,
            0.0,
        )
        if valid_count
        else 0.0
    )

    wrong_type_filenames = _wrong_type_filenames(case, parsed_for_type_checks)
    memory_type_accuracy = None
    if case.expected_types:
        memory_type_accuracy = _safe_div(
            len(case.expected_types) - len(wrong_type_filenames),
            len(case.expected_types),
            0.0,
        )

    return ExtractionMetrics(
        case_id=case.case_id,
        write_validity_rate=write_validity_rate,
        field_completeness_rate=field_completeness_rate,
        memory_type_accuracy=memory_type_accuracy,
        expected_memory_coverage=_safe_div(
            len(case.expected_memory_filenames & set(case.candidate_memory_files)),
            len(case.expected_memory_filenames),
            1.0,
        ),
        expected_fact_coverage=_safe_div(
            len(represented_expected_facts),
            len(case.expected_facts),
            1.0,
        ),
        grounding_rate=grounding_rate,
        noise_suppression_rate=noise_suppression_rate,
        forbidden_fact_leak_count=len(leaked_forbidden_facts),
        duplicate_control_rate=duplicate_control_rate,
        conflict_update_correctness=conflict_update_correctness,
        invalid_candidate_filenames=invalid_filenames,
        missing_expected_facts=missing_expected_facts,
        leaked_forbidden_facts=leaked_forbidden_facts,
        wrong_type_filenames=wrong_type_filenames,
        operations=operations,
    )


def memory_file(
    memory_type: str,
    name: str,
    description: str,
    content: str,
) -> str:
    return serialize_memory_file(
        MemoryEntry(
            name=name,
            description=description,
            content=content,
            metadata={"type": memory_type},
        )
    )


def _validate_case(case: ExtractionEvalCase) -> None:
    unknown_types = set(case.expected_types.values()) - _VALID_MEMORY_TYPES
    if unknown_types:
        raise ValueError(
            f"{case.case_id}: expected_types contains unknown memory types: "
            f"{sorted(unknown_types)}"
        )
    available_type_files = (
        set(case.existing_memory_files) | set(case.candidate_memory_files)
    )
    missing_type_files = set(case.expected_types) - available_type_files
    if missing_type_files:
        raise ValueError(
            f"{case.case_id}: expected_types references missing memory files: "
            f"{sorted(missing_type_files)}"
        )
    claims = case.candidate_claims or case.expected_facts
    unknown_evidence_claims = set(case.source_evidence) - claims
    if unknown_evidence_claims:
        raise ValueError(
            f"{case.case_id}: source_evidence references unknown claims: "
            f"{sorted(unknown_evidence_claims)}"
        )
    missing_update_files = (
        case.expected_updated_filenames - set(case.existing_memory_files)
    )
    if missing_update_files:
        raise ValueError(
            f"{case.case_id}: expected_updated_filenames missing from existing "
            f"memory files: {sorted(missing_update_files)}"
        )
    missing_delete_files = (
        case.expected_deleted_filenames - set(case.existing_memory_files)
    )
    if missing_delete_files:
        raise ValueError(
            f"{case.case_id}: expected_deleted_filenames missing from existing "
            f"memory files: {sorted(missing_delete_files)}"
        )


def _validate_candidate_expectations(
    case: ExtractionEvalCase,
    operations: OperationClassification,
) -> None:
    missing_updates = case.expected_updated_filenames - operations.updated_filenames
    if missing_updates:
        raise ValueError(
            f"{case.case_id}: expected updated files were not updated: "
            f"{sorted(missing_updates)}"
        )
    missing_deletes = case.expected_deleted_filenames - operations.deleted_filenames
    if missing_deletes:
        raise ValueError(
            f"{case.case_id}: expected deleted files were not deleted: "
            f"{sorted(missing_deletes)}"
        )


def _parse_candidate_files(
    case: ExtractionEvalCase,
) -> tuple[dict[str, MemoryEntry], set[str]]:
    return _parse_memory_files(case.candidate_memory_files)


def _parse_memory_files(
    memory_files: dict[str, str],
) -> tuple[dict[str, MemoryEntry], set[str]]:
    parsed: dict[str, MemoryEntry] = {}
    invalid: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        for filename, content in memory_files.items():
            path = memory_dir / filename
            path.write_text(content, encoding="utf-8")
            entry = parse_memory_file(path)
            if entry is None:
                invalid.add(filename)
            else:
                parsed[filename] = entry
    return parsed, invalid


def _entry_text(entry: MemoryEntry) -> str:
    return "\n".join([entry.name, entry.description, entry.content])


def _is_complete(entry: MemoryEntry) -> bool:
    return (
        bool(entry.name.strip())
        and bool(entry.description.strip())
        and bool(entry.content.strip())
        and entry.memory_type.value in _VALID_MEMORY_TYPES
    )


def _wrong_type_filenames(
    case: ExtractionEvalCase,
    parsed: dict[str, MemoryEntry],
) -> set[str]:
    wrong: set[str] = set()
    for filename, expected_type in case.expected_types.items():
        entry = parsed.get(filename)
        if entry is None or entry.memory_type.value != expected_type:
            wrong.add(filename)
    return wrong


def _represented_facts(facts: set[str], candidate_texts: list[str]) -> set[str]:
    """Return facts whose tokens appear in one candidate memory.

    Phase one uses token-subset matching. Token order and negation are not
    considered. False positives on token-identical but semantically opposite
    texts are a known limitation.
    """
    represented: set[str] = set()
    candidate_token_sets = [_tokens(text) for text in candidate_texts]
    for fact in facts:
        fact_tokens = _tokens(fact)
        if fact_tokens and any(
            fact_tokens.issubset(candidate_tokens)
            for candidate_tokens in candidate_token_sets
        ):
            represented.add(fact)
        elif not fact_tokens and not fact.strip():
            represented.add(fact)
    return represented


def _grounding_rate(case: ExtractionEvalCase) -> float:
    claims = case.candidate_claims or case.expected_facts
    if not claims:
        return 1.0
    conversation_tokens = _tokens(flatten_conversation_text(case.conversation))
    grounded = 0
    for claim in claims:
        evidence_values = case.source_evidence.get(claim, {claim})
        if any(
            _tokens(evidence).issubset(conversation_tokens)
            for evidence in evidence_values
        ):
            grounded += 1
    return _safe_div(grounded, len(claims), 1.0)


def _duplicate_control_rate(
    case: ExtractionEvalCase,
    operations: OperationClassification,
    candidate_text_by_file: dict[str, str],
) -> float | None:
    if not case.duplicate_facts:
        return None
    created_texts = [
        candidate_text_by_file.get(filename, "")
        for filename in operations.created_filenames
    ]
    leaked_duplicates = _represented_facts(case.duplicate_facts, created_texts)
    return 0.0 if leaked_duplicates else 1.0


def _conflict_update_correctness(
    case: ExtractionEvalCase,
    candidate_texts: list[str],
) -> float | None:
    if not case.expected_latest_facts and not case.obsolete_facts:
        return None
    latest_present = (
        _represented_facts(case.expected_latest_facts, candidate_texts)
        == case.expected_latest_facts
    )
    obsolete_present = bool(_represented_facts(case.obsolete_facts, candidate_texts))
    return 1.0 if latest_present and not obsolete_present else 0.0


def _safe_div(numerator: int, denominator: int, empty_value: float) -> float:
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
