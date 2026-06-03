# Memory Extraction Quality Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, CI-safe benchmark that measures memory extraction output quality independently from live LLM extraction behavior.

**Architecture:** Keep the evaluation test-local. Add `tests/memory/helpers/extraction_eval.py` with case models, memory parsing, operation classification, lexical fact matching, metrics, scorecard aggregation, and compact formatting. Add `tests/memory/test_extraction_eval.py` with TDD coverage for validity, coverage, grounding, noise, duplicate, conflict, update/delete expectations, curated cases, and reporting.

**Tech Stack:** Python 3.11, pytest, dataclasses, pathlib, existing `xxcode.memory.models.parse_memory_file` and `xxcode.memory.models.MemoryType`

---

## File Structure

- Create: `tests/memory/helpers/extraction_eval.py`
  Responsibility: extraction evaluation dataclasses, candidate memory materialization/parsing, operation classification, conversation flattening, lexical matching, metric computation, curated cases, scorecards, and compact report formatting.
- Create: `tests/memory/test_extraction_eval.py`
  Responsibility: deterministic TDD coverage for extraction output quality metrics and benchmark scorecards.
- Reuse without modification: `src/xxcode/memory/models.py`
  Responsibility: production `parse_memory_file(...)`, `MemoryEntry`, and `MemoryType` behavior under evaluation.
- Reuse without modification: `tests/memory/helpers/__init__.py`
  Responsibility: existing test helper package marker.

## Task 1: Add Core Case Model, Parsing, Operations, And Validity Metrics

**Files:**
- Create: `tests/memory/helpers/extraction_eval.py`
- Create: `tests/memory/test_extraction_eval.py`

- [ ] **Step 1: Write failing tests for conversation flattening, operation classification, and validity metrics**

Create `tests/memory/test_extraction_eval.py` with:

```python
import pytest

from tests.memory.helpers.extraction_eval import (
    ExtractionEvalCase,
    classify_operations,
    compute_extraction_metrics,
    flatten_conversation_text,
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
            "python-style.md": _memory_file(
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


def test_unparseable_candidate_counts_as_invalid_and_incomplete():
    case = ExtractionEvalCase(
        case_id="invalid-candidate",
        conversation=[],
        existing_memory_files={},
        candidate_memory_files={
            "broken.md": "Body without YAML frontmatter\n",
        },
        expected_memory_filenames=set(),
        expected_facts=set(),
        expected_types={},
    )

    metrics = compute_extraction_metrics(case)

    assert metrics.write_validity_rate == 0.0
    assert metrics.field_completeness_rate == 0.0
    assert metrics.memory_type_accuracy is None
    assert metrics.invalid_candidate_filenames == {"broken.md"}


def _memory_file(
    memory_type: str,
    name: str,
    description: str,
    content: str,
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{content}\n"
    )
```

- [ ] **Step 2: Run the new extraction evaluation tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py -v
```

Expected: FAIL with `ModuleNotFoundError` because `tests.memory.helpers.extraction_eval` does not exist.

- [ ] **Step 3: Add core helper implementation**

Create `tests/memory/helpers/extraction_eval.py` with:

```python
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xxcode.memory.models import (
    MemoryEntry,
    MemoryType,
    _parse_frontmatter,
    parse_memory_file,
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
    parsed, invalid_filenames = _parse_candidate_files(case)
    candidate_text_by_file = {
        filename: _entry_text(entry) for filename, entry in parsed.items()
    }

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

    wrong_type_filenames = _wrong_type_filenames(case, parsed)
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
        expected_fact_coverage=1.0,
        grounding_rate=1.0,
        noise_suppression_rate=None,
        forbidden_fact_leak_count=0,
        duplicate_control_rate=None,
        conflict_update_correctness=None,
        invalid_candidate_filenames=invalid_filenames,
        missing_expected_facts=set(),
        leaked_forbidden_facts=set(),
        wrong_type_filenames=wrong_type_filenames,
        operations=operations,
    )


def _validate_case(case: ExtractionEvalCase) -> None:
    unknown_types = set(case.expected_types.values()) - _VALID_MEMORY_TYPES
    if unknown_types:
        raise ValueError(
            f"{case.case_id}: expected_types contains unknown memory types: "
            f"{sorted(unknown_types)}"
        )
    missing_type_files = set(case.expected_types) - set(case.candidate_memory_files)
    if missing_type_files:
        raise ValueError(
            f"{case.case_id}: expected_types references missing candidate files: "
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


def _parse_candidate_files(
    case: ExtractionEvalCase,
) -> tuple[dict[str, MemoryEntry], set[str]]:
    parsed: dict[str, MemoryEntry] = {}
    invalid: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        for filename, content in case.candidate_memory_files.items():
            path = memory_dir / filename
            path.write_text(content, encoding="utf-8")
            entry = parse_memory_file(path)
            if entry is None or not _has_parseable_frontmatter(content):
                invalid.add(filename)
            else:
                parsed[filename] = entry
    return parsed, invalid


def _entry_text(entry: MemoryEntry) -> str:
    return "\n".join([entry.name, entry.description, entry.content])


def _has_parseable_frontmatter(content: str) -> bool:
    metadata, _body = _parse_frontmatter(content)
    return bool(metadata)


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


def _safe_div(numerator: int, denominator: int, empty_value: float) -> float:
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
```

- [ ] **Step 4: Run extraction evaluation tests to verify Task 1 passes**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py -v
```

Expected: PASS with 4 tests.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
git commit -m "Add memory extraction evaluation validity metrics"
```

## Task 2: Add Lexical Fact Coverage, Grounding, And Noise Metrics

**Files:**
- Modify: `tests/memory/helpers/extraction_eval.py`
- Modify: `tests/memory/test_extraction_eval.py`

- [ ] **Step 1: Write failing tests for fact coverage, grounding, and forbidden fact leakage**

Append this to `tests/memory/test_extraction_eval.py`:

```python
def test_expected_fact_coverage_uses_lexical_token_matching():
    case = ExtractionEvalCase(
        case_id="fact-coverage",
        conversation=[
            {"role": "user", "content": "I prefer snake_case in Python tests."},
        ],
        existing_memory_files={},
        candidate_memory_files={
            "python-style.md": _memory_file(
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
            "analysis-style.md": _memory_file(
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
            "path-style.md": _memory_file(
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
            "debug-note.md": _memory_file(
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
```

- [ ] **Step 2: Run Task 2 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py::test_expected_fact_coverage_uses_lexical_token_matching tests/memory/test_extraction_eval.py::test_grounding_rate_uses_claim_text_as_default_evidence tests/memory/test_extraction_eval.py::test_grounding_rate_uses_source_evidence_override tests/memory/test_extraction_eval.py::test_forbidden_fact_leak_reduces_noise_suppression_rate tests/memory/test_extraction_eval.py::test_empty_forbidden_facts_excludes_noise_suppression_rate -v
```

Expected: FAIL because Task 1 helper returns fixed coverage, grounding, and noise values that do not yet inspect candidate content.

- [ ] **Step 3: Implement lexical matching, grounding, and noise metrics**

Update `compute_extraction_metrics(...)` in `tests/memory/helpers/extraction_eval.py` by replacing the temporary coverage, grounding, and noise fields with local variables:

```python
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
```

Then change the `ExtractionMetrics(...)` call in the same function to:

```python
        expected_fact_coverage=_safe_div(
            len(represented_expected_facts),
            len(case.expected_facts),
            1.0,
        ),
        grounding_rate=grounding_rate,
        noise_suppression_rate=noise_suppression_rate,
        forbidden_fact_leak_count=len(leaked_forbidden_facts),
        duplicate_control_rate=None,
        conflict_update_correctness=None,
        invalid_candidate_filenames=invalid_filenames,
        missing_expected_facts=missing_expected_facts,
        leaked_forbidden_facts=leaked_forbidden_facts,
```

Add these helper functions below `_wrong_type_filenames(...)`:

```python
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
        if any(_tokens(evidence).issubset(conversation_tokens) for evidence in evidence_values):
            grounded += 1
    return _safe_div(grounded, len(claims), 1.0)
```

- [ ] **Step 4: Run extraction evaluation tests to verify Task 2 passes**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py -v
```

Expected: PASS with 9 tests.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
git commit -m "Add memory extraction coverage and grounding metrics"
```

## Task 3: Add Duplicate, Conflict, Update, And Delete Expectation Metrics

**Files:**
- Modify: `tests/memory/helpers/extraction_eval.py`
- Modify: `tests/memory/test_extraction_eval.py`

- [ ] **Step 1: Write failing tests for duplicate and conflict metrics**

Append this to `tests/memory/test_extraction_eval.py`:

```python
def test_duplicate_control_checks_only_newly_created_files():
    existing = _memory_file(
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
            "duplicate-style.md": _memory_file(
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
    existing = _memory_file(
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
        case_id="conflict-update",
        conversation=[
            {"role": "user", "content": "Actually use pathlib instead of os.path."},
        ],
        existing_memory_files={
            "path-style.md": _memory_file(
                "feedback",
                "Path Style",
                "Prefer os.path",
                "Prefer os.path for path manipulation.",
            ),
        },
        candidate_memory_files={
            "path-style.md": _memory_file(
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
    assert metrics.operations.updated_filenames == {"path-style.md"}


def test_conflict_update_correctness_fails_when_obsolete_fact_remains():
    case = ExtractionEvalCase(
        case_id="conflict-obsolete-retained",
        conversation=[
            {"role": "user", "content": "Actually use pathlib instead of os.path."},
        ],
        existing_memory_files={
            "path-style.md": _memory_file(
                "feedback",
                "Path Style",
                "Prefer os.path",
                "Prefer os.path for path manipulation.",
            ),
        },
        candidate_memory_files={
            "path-style.md": _memory_file(
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
```

- [ ] **Step 2: Write failing tests for update/delete expectation validation**

Append this to `tests/memory/test_extraction_eval.py`:

```python
def test_expected_updated_filename_must_be_classified_as_updated():
    unchanged = _memory_file(
        "project",
        "Project Rule",
        "Always run tests",
        "Always run tests before reporting completion.",
    )
    case = ExtractionEvalCase(
        case_id="expected-update-not-updated",
        conversation=[],
        existing_memory_files={"project-rule.md": unchanged},
        candidate_memory_files={"project-rule.md": unchanged},
        expected_memory_filenames={"project-rule.md"},
        expected_facts={"always run tests"},
        expected_types={"project-rule.md": "project"},
        expected_updated_filenames={"project-rule.md"},
    )

    with pytest.raises(ValueError, match="expected updated files were not updated"):
        compute_extraction_metrics(case)


def test_expected_deleted_filename_must_be_classified_as_deleted():
    existing = _memory_file(
        "project",
        "Temporary Note",
        "Temporary debug note",
        "Temporary debug note.",
    )
    case = ExtractionEvalCase(
        case_id="expected-delete-not-deleted",
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
```

- [ ] **Step 3: Run Task 3 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py::test_duplicate_control_checks_only_newly_created_files tests/memory/test_extraction_eval.py::test_duplicate_control_passes_when_duplicate_fact_is_not_in_created_file tests/memory/test_extraction_eval.py::test_conflict_update_correctness_requires_latest_and_no_obsolete_fact tests/memory/test_extraction_eval.py::test_conflict_update_correctness_fails_when_obsolete_fact_remains tests/memory/test_extraction_eval.py::test_expected_updated_filename_must_be_classified_as_updated tests/memory/test_extraction_eval.py::test_expected_deleted_filename_must_be_classified_as_deleted -v
```

Expected: FAIL because duplicate/conflict metrics still return `None` and update/delete expectation validation is not implemented.

- [ ] **Step 4: Implement duplicate, conflict, and output expectation validation**

Add this call in `compute_extraction_metrics(...)` immediately after `operations = classify_operations(...)`:

```python
    _validate_candidate_expectations(case, operations)
```

Add these local variables after `candidate_text_by_file = {...}`:

```python
    duplicate_control_rate = _duplicate_control_rate(
        case,
        operations,
        candidate_text_by_file,
    )
    conflict_update_correctness = _conflict_update_correctness(
        case,
        list(candidate_text_by_file.values()),
    )
```

Replace the duplicate/conflict fields in `ExtractionMetrics(...)` with:

```python
        duplicate_control_rate=duplicate_control_rate,
        conflict_update_correctness=conflict_update_correctness,
```

Add these helper functions below `_validate_case(...)`:

```python
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
```

Add these helper functions below `_grounding_rate(...)`:

```python
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
```

- [ ] **Step 5: Run extraction evaluation tests to verify Task 3 passes**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py -v
```

Expected: PASS with 15 tests.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
git commit -m "Add memory extraction duplicate and conflict metrics"
```

## Task 4: Add Curated Benchmark Cases, Scorecard Aggregation, And Reporting

**Files:**
- Modify: `tests/memory/helpers/extraction_eval.py`
- Modify: `tests/memory/test_extraction_eval.py`

- [ ] **Step 1: Write failing tests for curated cases and scorecard aggregation**

Append this import to the import list in `tests/memory/test_extraction_eval.py`:

```python
    build_extraction_scorecard,
    extraction_quality_cases,
    format_extraction_scorecard,
```

Append these tests to `tests/memory/test_extraction_eval.py`:

```python
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
```

- [ ] **Step 2: Run Task 4 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py::test_extraction_quality_cases_have_expected_positive_metrics tests/memory/test_extraction_eval.py::test_extraction_quality_risk_cases_expose_expected_weaknesses tests/memory/test_extraction_eval.py::test_extraction_scorecard_excludes_none_metrics_from_optional_means tests/memory/test_extraction_eval.py::test_extraction_scorecard_summary_includes_key_metrics -v
```

Expected: FAIL because scorecard helpers and curated cases do not exist.

- [ ] **Step 3: Add scorecard dataclass, curated cases, and formatting**

Add this dataclass below `ExtractionMetrics` in `tests/memory/helpers/extraction_eval.py`:

```python
@dataclass(frozen=True)
class ExtractionScorecard:
    n_cases: int
    mean_write_validity_rate: float
    mean_field_completeness_rate: float
    mean_expected_memory_coverage: float
    mean_expected_fact_coverage: float
    mean_grounding_rate: float
    n_noise_cases: int
    mean_noise_suppression_rate: float
    total_forbidden_fact_leak_count: int
    n_type_cases: int
    mean_memory_type_accuracy: float
    n_duplicate_cases: int
    mean_duplicate_control_rate: float
    n_conflict_cases: int
    mean_conflict_update_correctness: float
```

Add these functions above `_validate_case(...)`:

```python
def extraction_quality_cases() -> list[ExtractionEvalCase]:
    existing_duplicate = _memory_file(
        "user",
        "Existing Style",
        "User prefers snake_case",
        "User prefers snake_case in Python tests.",
    )
    return [
        ExtractionEvalCase(
            case_id="captures-user-style",
            conversation=[
                {"role": "user", "content": "I prefer snake_case in Python tests."},
            ],
            existing_memory_files={},
            candidate_memory_files={
                "python-style.md": _memory_file(
                    "user",
                    "Python Style",
                    "User prefers snake_case in Python tests",
                    "Use snake_case when writing Python tests for the user.",
                ),
            },
            expected_memory_filenames={"python-style.md"},
            expected_facts={"user prefers snake_case python tests"},
            expected_types={"python-style.md": "user"},
        ),
        ExtractionEvalCase(
            case_id="rejects-temporary-debug-noise",
            conversation=[
                {"role": "user", "content": "Temporarily debug port 5432 today."},
            ],
            existing_memory_files={},
            candidate_memory_files={},
            expected_memory_filenames=set(),
            expected_facts=set(),
            expected_types={},
            forbidden_facts={"temporary debug port 5432"},
        ),
        ExtractionEvalCase(
            case_id="wrong-type-risk",
            conversation=[
                {"role": "user", "content": "Always run tests before completion."},
            ],
            existing_memory_files={},
            candidate_memory_files={
                "testing-rule.md": _memory_file(
                    "reference",
                    "Testing Rule",
                    "Always run tests",
                    "Always run tests before completion.",
                ),
            },
            expected_memory_filenames={"testing-rule.md"},
            expected_facts={"always run tests"},
            expected_types={"testing-rule.md": "feedback"},
        ),
        ExtractionEvalCase(
            case_id="missing-fact-risk",
            conversation=[
                {"role": "user", "content": "Use pandas and prefer pytest fixtures."},
            ],
            existing_memory_files={},
            candidate_memory_files={
                "analysis-style.md": _memory_file(
                    "user",
                    "Analysis Style",
                    "Use pandas",
                    "Use pandas for analysis.",
                ),
            },
            expected_memory_filenames={"analysis-style.md"},
            expected_facts={"use pandas", "prefer pytest fixtures"},
            expected_types={"analysis-style.md": "user"},
        ),
        ExtractionEvalCase(
            case_id="ungrounded-risk",
            conversation=[
                {"role": "user", "content": "Use pandas dataframes for analysis."},
            ],
            existing_memory_files={},
            candidate_memory_files={
                "hallucinated-style.md": _memory_file(
                    "user",
                    "Hallucinated Style",
                    "Use spark clusters",
                    "Use spark clusters for analysis.",
                ),
            },
            expected_memory_filenames={"hallucinated-style.md"},
            expected_facts={"use spark clusters"},
            expected_types={"hallucinated-style.md": "user"},
            candidate_claims={"use spark clusters"},
        ),
        ExtractionEvalCase(
            case_id="duplicate-risk",
            conversation=[
                {"role": "user", "content": "I prefer snake_case in Python tests."},
            ],
            existing_memory_files={"existing-style.md": existing_duplicate},
            candidate_memory_files={
                "existing-style.md": existing_duplicate,
                "duplicate-style.md": _memory_file(
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
        ),
        ExtractionEvalCase(
            case_id="conflict-risk",
            conversation=[
                {"role": "user", "content": "Actually use pathlib instead of os.path."},
            ],
            existing_memory_files={
                "path-style.md": _memory_file(
                    "feedback",
                    "Path Style",
                    "Prefer os.path",
                    "Prefer os.path for path manipulation.",
                ),
            },
            candidate_memory_files={
                "path-style.md": _memory_file(
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
        ),
    ]


def build_extraction_scorecard(
    metrics: list[ExtractionMetrics],
) -> ExtractionScorecard:
    if not metrics:
        return ExtractionScorecard(
            n_cases=0,
            mean_write_validity_rate=0.0,
            mean_field_completeness_rate=0.0,
            mean_expected_memory_coverage=0.0,
            mean_expected_fact_coverage=0.0,
            mean_grounding_rate=0.0,
            n_noise_cases=0,
            mean_noise_suppression_rate=0.0,
            total_forbidden_fact_leak_count=0,
            n_type_cases=0,
            mean_memory_type_accuracy=0.0,
            n_duplicate_cases=0,
            mean_duplicate_control_rate=0.0,
            n_conflict_cases=0,
            mean_conflict_update_correctness=0.0,
        )
    n_cases = len(metrics)
    type_values = [m.memory_type_accuracy for m in metrics if m.memory_type_accuracy is not None]
    noise_values = [m.noise_suppression_rate for m in metrics if m.noise_suppression_rate is not None]
    duplicate_values = [m.duplicate_control_rate for m in metrics if m.duplicate_control_rate is not None]
    conflict_values = [m.conflict_update_correctness for m in metrics if m.conflict_update_correctness is not None]
    return ExtractionScorecard(
        n_cases=n_cases,
        mean_write_validity_rate=sum(m.write_validity_rate for m in metrics) / n_cases,
        mean_field_completeness_rate=sum(m.field_completeness_rate for m in metrics) / n_cases,
        mean_expected_memory_coverage=sum(m.expected_memory_coverage for m in metrics) / n_cases,
        mean_expected_fact_coverage=sum(m.expected_fact_coverage for m in metrics) / n_cases,
        mean_grounding_rate=sum(m.grounding_rate for m in metrics) / n_cases,
        n_noise_cases=len(noise_values),
        mean_noise_suppression_rate=_mean_optional(noise_values),
        total_forbidden_fact_leak_count=sum(m.forbidden_fact_leak_count for m in metrics),
        n_type_cases=len(type_values),
        mean_memory_type_accuracy=_mean_optional(type_values),
        n_duplicate_cases=len(duplicate_values),
        mean_duplicate_control_rate=_mean_optional(duplicate_values),
        n_conflict_cases=len(conflict_values),
        mean_conflict_update_correctness=_mean_optional(conflict_values),
    )


def format_extraction_scorecard(scorecard: ExtractionScorecard) -> str:
    return (
        "extraction "
        f"n_cases={scorecard.n_cases} "
        f"mean_write_validity_rate={scorecard.mean_write_validity_rate:.3f} "
        f"mean_field_completeness_rate={scorecard.mean_field_completeness_rate:.3f} "
        f"mean_expected_memory_coverage={scorecard.mean_expected_memory_coverage:.3f} "
        f"mean_expected_fact_coverage={scorecard.mean_expected_fact_coverage:.3f} "
        f"mean_grounding_rate={scorecard.mean_grounding_rate:.3f} "
        f"n_noise_cases={scorecard.n_noise_cases} "
        f"mean_noise_suppression_rate={scorecard.mean_noise_suppression_rate:.3f} "
        f"n_type_cases={scorecard.n_type_cases} "
        f"mean_memory_type_accuracy={scorecard.mean_memory_type_accuracy:.3f} "
        f"n_duplicate_cases={scorecard.n_duplicate_cases} "
        f"mean_duplicate_control_rate={scorecard.mean_duplicate_control_rate:.3f} "
        f"n_conflict_cases={scorecard.n_conflict_cases} "
        f"mean_conflict_update_correctness={scorecard.mean_conflict_update_correctness:.3f}"
    )
```

Add these helper functions near the bottom of `tests/memory/helpers/extraction_eval.py`:

```python
def _mean_optional(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _memory_file(
    memory_type: str,
    name: str,
    description: str,
    content: str,
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{content}\n"
    )
```

- [ ] **Step 4: Run extraction evaluation tests to verify Task 4 passes**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction_eval.py -v
```

Expected: PASS with 19 tests.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
git commit -m "Report deterministic memory extraction quality scorecards"
```

## Task 5: Final Verification

**Files:**
- Verify: `tests/memory/helpers/extraction_eval.py`
- Verify: `tests/memory/test_extraction_eval.py`
- Verify: existing memory evaluation suites

- [ ] **Step 1: Run focused extraction and existing evaluation suites**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_extraction.py tests/memory/test_extraction_prompt.py tests/memory/test_extraction_eval.py tests/memory/test_index_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full memory suite**

Run:

```powershell
py -3.11 -m pytest tests/memory -v
```

Expected: PASS.

- [ ] **Step 3: Run context suite regression**

Run:

```powershell
py -3.11 -m pytest tests/context -v
```

Expected: PASS.

- [ ] **Step 4: Inspect task-specific diff**

Run:

```powershell
git diff -- tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
```

Expected: no output if all implementation tasks were committed separately. If output exists, inspect it and either commit task-specific changes or fix unintended edits.

- [ ] **Step 5: Inspect task-specific status**

Run:

```powershell
git status --short -- tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
```

Expected: no output if all implementation tasks were committed separately.

- [ ] **Step 6: Commit final batched changes only if previous task commits were skipped**

If implementation changes were intentionally batched and are still unstaged, run:

```bash
git add tests/memory/helpers/extraction_eval.py tests/memory/test_extraction_eval.py
git commit -m "Add deterministic memory extraction quality evaluation suite"
```
