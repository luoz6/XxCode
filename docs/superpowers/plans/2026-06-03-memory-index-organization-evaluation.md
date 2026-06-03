# Memory Index Organization Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, CI-safe benchmark that measures `MEMORY.md` index organization quality independently from recall quality.

**Architecture:** Keep the evaluation test-local. Add `tests/memory/helpers/index_eval.py` with generated/raw case dataclasses, raw markdown-link scanning, metric computation, scorecard aggregation, and compact formatting. Add `tests/memory/test_index_eval.py` with TDD coverage for raw risk cases, healthy generated cases, description quality, budget metrics, and regression commands.

**Tech Stack:** Python 3.11, pytest, dataclasses, pathlib, existing `xxcode.memory.index`, `xxcode.memory.models`, and `xxcode.memory.store` APIs

---

## File Structure

- Create: `tests/memory/helpers/index_eval.py`
  Responsibility: index evaluation case models, temporary memory materialization, raw link scanning, structural metrics, description metrics, budget metrics, ordering metrics, scorecards, and summary formatting.
- Create: `tests/memory/test_index_eval.py`
  Responsibility: TDD coverage for raw scanner behavior, generated healthy index cases, raw risk cases, description metrics, budget metrics, scorecards, and summary formatting.
- Reuse: `tests/memory/helpers/__init__.py`
  Responsibility: existing test helper package marker.
- Reuse without modification: `src/xxcode/memory/index.py`
  Responsibility: production parser, generator, truncation constants, and runtime index behavior under evaluation.
- Reuse without modification: `src/xxcode/memory/store.py`
  Responsibility: generate real memory files from `MemoryEntry` for generated index cases.
- Reuse without modification: `src/xxcode/memory/models.py`
  Responsibility: `MemoryEntry` model and serialized memory frontmatter behavior.

## Task 1: Add Raw Link Scanner And Structural Metrics

**Files:**
- Create: `tests/memory/helpers/index_eval.py`
- Create: `tests/memory/test_index_eval.py`

- [ ] **Step 1: Write failing tests for raw link scanning and structural risk metrics**

Create `tests/memory/test_index_eval.py` with:

```python
from tests.memory.helpers.index_eval import (
    RawIndexEvalCase,
    compute_index_metrics,
    scan_raw_index_links,
)


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

    metrics = compute_index_metrics(case.index_content, case.memory_files)

    assert metrics.case_id == "raw-structural-risk"
    assert metrics.indexed_file_count == 2
    assert metrics.memory_file_count == 1
    assert metrics.coverage_rate == 1.0
    assert metrics.stale_reference_rate == 1 / 4
    assert metrics.duplicate_reference_rate == 1 / 4
    assert metrics.parseable_line_rate == 1.0
    assert metrics.memory_md_exclusion == 0.0


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
```

- [ ] **Step 2: Run the raw scanner tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py -v
```

Expected: FAIL with `ModuleNotFoundError` because `tests.memory.helpers.index_eval` does not exist.

- [ ] **Step 3: Add raw scanner and structural metric implementation**

Create `tests/memory/helpers/index_eval.py` with:

```python
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from xxcode.memory.index import (
    INDEX_FILENAME,
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    parse_memory_index,
    truncate_entrypoint_content,
)


_RAW_LINK_RE = re.compile(
    r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)\s*(?P<tail>.*)$"
)
_GENERIC_DESCRIPTIONS = {
    "general",
    "information",
    "memory",
    "misc",
    "note",
    "stuff",
    "todo",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RawIndexLink:
    title: str
    filename: str
    description: str
    line: str


@dataclass(frozen=True)
class RawIndexEvalCase:
    case_id: str
    index_content: str
    memory_files: dict[str, str]
    expected_present_filenames: set[str]
    risk_labels: set[str] | None = None


@dataclass(frozen=True)
class IndexOrganizationMetrics:
    case_id: str
    indexed_file_count: int
    memory_file_count: int
    coverage_rate: float
    stale_reference_rate: float
    duplicate_reference_rate: float
    parseable_line_rate: float
    memory_md_exclusion: float
    description_present_rate: float
    description_budget_compliance_rate: float
    generic_description_rate: float
    discriminative_token_rate: float
    line_utilization: float
    byte_utilization: float
    was_line_truncated: bool
    was_byte_truncated: bool
    type_order_adherence: float | None = None


def scan_raw_index_links(index_content: str) -> list[RawIndexLink]:
    links: list[RawIndexLink] = []
    for line in index_content.splitlines():
        match = _RAW_LINK_RE.match(line)
        if not match:
            continue
        tail = match.group("tail").strip()
        description = re.sub(r"^(?:[-:]\s*)+", "", tail).strip()
        links.append(
            RawIndexLink(
                title=match.group("title").strip(),
                filename=Path(match.group("file")).name,
                description=description,
                line=line,
            )
        )
    return links


def compute_index_metrics(
    index_content: str,
    memory_files: dict[str, str],
    *,
    case_id: str = "index-case",
    type_order_adherence: float | None = None,
) -> IndexOrganizationMetrics:
    raw_links = scan_raw_index_links(index_content)
    runtime_entries = parse_memory_index(index_content)
    raw_filenames = [link.filename for link in raw_links]
    raw_reference_count = len(raw_filenames)
    memory_filenames = {
        filename for filename in memory_files if filename != INDEX_FILENAME
    }
    parsed_filenames = {
        entry.filename for entry in runtime_entries if entry.filename != INDEX_FILENAME
    }
    existing_indexed = parsed_filenames & memory_filenames

    stale_references = [
        filename
        for filename in raw_filenames
        if filename != INDEX_FILENAME and filename not in memory_filenames
    ]
    duplicate_references = _duplicate_count(raw_filenames)
    candidate_lines = [
        line for line in index_content.splitlines() if line.lstrip().startswith("-")
    ]
    parseable_line_count = len(raw_links)
    truncation = truncate_entrypoint_content(index_content)

    return IndexOrganizationMetrics(
        case_id=case_id,
        indexed_file_count=len(parsed_filenames),
        memory_file_count=len(memory_filenames),
        coverage_rate=_safe_div(len(existing_indexed), len(memory_filenames), 1.0),
        stale_reference_rate=_safe_div(len(stale_references), raw_reference_count, 0.0),
        duplicate_reference_rate=_safe_div(
            duplicate_references,
            raw_reference_count,
            0.0,
        ),
        parseable_line_rate=_safe_div(
            parseable_line_count,
            len(candidate_lines),
            1.0,
        ),
        memory_md_exclusion=0.0 if INDEX_FILENAME in raw_filenames else 1.0,
        description_present_rate=_description_present_rate(runtime_entries),
        description_budget_compliance_rate=_description_budget_compliance_rate(raw_links),
        generic_description_rate=_generic_description_rate(runtime_entries),
        discriminative_token_rate=_discriminative_token_rate(runtime_entries),
        line_utilization=truncation.line_count / MAX_ENTRYPOINT_LINES,
        byte_utilization=truncation.byte_count / MAX_ENTRYPOINT_BYTES,
        was_line_truncated=truncation.was_line_truncated,
        was_byte_truncated=truncation.was_byte_truncated,
        type_order_adherence=type_order_adherence,
    )


def _duplicate_count(values: list[str]) -> int:
    return sum(count - 1 for count in Counter(values).values() if count > 1)


def _safe_div(numerator: int, denominator: int, empty_value: float) -> float:
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _description_present_rate(entries) -> float:
    return _safe_div(
        sum(1 for entry in entries if entry.description.strip()),
        len(entries),
        1.0,
    )


def _description_budget_compliance_rate(raw_links: list[RawIndexLink]) -> float:
    from xxcode.memory import index as index_module

    line_budget = getattr(index_module, "_LINE_BUDGET", 150)
    return _safe_div(
        sum(1 for link in raw_links if len(link.line) <= line_budget),
        len(raw_links),
        1.0,
    )


def _generic_description_rate(entries) -> float:
    generic = 0
    for entry in entries:
        normalized = entry.description.strip().lower()
        if normalized in _GENERIC_DESCRIPTIONS:
            generic += 1
    return _safe_div(generic, len(entries), 0.0)


def _discriminative_token_rate(entries) -> float:
    discriminative = 0
    for entry in entries:
        filename_tokens = _tokens(Path(entry.filename).stem.replace("-", " "))
        description_tokens = _tokens(entry.description)
        if description_tokens - filename_tokens:
            discriminative += 1
    return _safe_div(discriminative, len(entries), 1.0)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
```

- [ ] **Step 4: Run the raw scanner tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
git commit -m "Add memory index organization structural metrics"
```

## Task 2: Add Generated Healthy Index Cases And Type Ordering

**Files:**
- Modify: `tests/memory/helpers/index_eval.py`
- Modify: `tests/memory/test_index_eval.py`

- [ ] **Step 1: Write failing generated index tests**

Append this to `tests/memory/test_index_eval.py`:

```python
from tests.memory.helpers.index_eval import (
    GeneratedIndexEvalCase,
    build_index_scorecard,
    compute_generated_index_metrics,
    generated_index_cases,
)


def test_generated_index_case_reports_healthy_structure(tmp_path):
    case = GeneratedIndexEvalCase(
        case_id="healthy-generated",
        memory_files={
            "user-style.md": _memory_file("user", "User Style", "User prefers pytest"),
            "project-plan.md": _memory_file("project", "Project Plan", "Project release plan"),
            "feedback-rule.md": _memory_file("feedback", "Feedback Rule", "Always run tests"),
            "reference-doc.md": _memory_file("reference", "Reference Doc", "External docs"),
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

    assert scorecard.n_cases == len(generated_index_cases())
    assert scorecard.mean_coverage_rate == 1.0
    assert scorecard.mean_stale_reference_rate == 0.0
    assert scorecard.type_order_adherence_rate == 1.0
```

- [ ] **Step 2: Run generated index tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py::test_generated_index_case_reports_healthy_structure tests/memory/test_index_eval.py::test_generated_index_scorecard_reports_case_count -v
```

Expected: FAIL because generated case helpers and scorecard do not exist.

- [ ] **Step 3: Add generated case materialization and scorecard implementation**

Append this to `tests/memory/helpers/index_eval.py`:

```python
from xxcode.memory.index import generate_memory_index
from xxcode.memory.models import MemoryEntry, parse_memory_file
from xxcode.memory.store import MemoryStore


@dataclass(frozen=True)
class GeneratedIndexEvalCase:
    case_id: str
    memory_files: dict[str, str]
    expected_indexed_filenames: set[str]
    expected_type_order: list[str] | None = None


@dataclass(frozen=True)
class IndexOrganizationScorecard:
    n_cases: int
    mean_coverage_rate: float
    mean_stale_reference_rate: float
    mean_duplicate_reference_rate: float
    mean_parseable_line_rate: float
    mean_description_present_rate: float
    mean_description_budget_compliance_rate: float
    mean_generic_description_rate: float
    mean_discriminative_token_rate: float
    mean_line_utilization: float
    mean_byte_utilization: float
    truncated_case_count: int
    type_order_adherence_rate: float


def compute_generated_index_metrics(
    case: GeneratedIndexEvalCase,
    memory_dir: Path,
) -> IndexOrganizationMetrics:
    _materialize_generated_case(case, memory_dir)
    index_content = generate_memory_index(memory_dir)
    type_order = _type_order_adherence(index_content, memory_dir, case.expected_type_order)
    metrics = compute_index_metrics(
        index_content,
        case.memory_files,
        case_id=case.case_id,
        type_order_adherence=type_order,
    )
    missing = case.expected_indexed_filenames - {
        link.filename for link in scan_raw_index_links(index_content)
    }
    if missing:
        raise AssertionError(f"{case.case_id}: missing indexed files: {sorted(missing)}")
    return metrics


def generated_index_cases() -> list[GeneratedIndexEvalCase]:
    return [
        GeneratedIndexEvalCase(
            case_id="generated-type-order",
            memory_files={
                "reference-doc.md": _memory_file("reference", "Reference Doc", "External docs"),
                "feedback-rule.md": _memory_file("feedback", "Feedback Rule", "Always run tests"),
                "project-plan.md": _memory_file("project", "Project Plan", "Project release plan"),
                "user-style.md": _memory_file("user", "User Style", "User prefers pytest"),
            },
            expected_indexed_filenames={
                "user-style.md",
                "project-plan.md",
                "feedback-rule.md",
                "reference-doc.md",
            },
            expected_type_order=["user", "project", "feedback", "reference"],
        ),
        GeneratedIndexEvalCase(
            case_id="generated-description-signal",
            memory_files={
                "pandas-style.md": _memory_file("user", "Pandas Style", "User prefers pandas dataframes"),
                "recall-benchmark.md": _memory_file("project", "Recall Benchmark", "Recall benchmark metrics"),
            },
            expected_indexed_filenames={"pandas-style.md", "recall-benchmark.md"},
            expected_type_order=["user", "project"],
        ),
    ]


def build_index_scorecard(
    metrics: list[IndexOrganizationMetrics],
) -> IndexOrganizationScorecard:
    if not metrics:
        return IndexOrganizationScorecard(
            n_cases=0,
            mean_coverage_rate=0.0,
            mean_stale_reference_rate=0.0,
            mean_duplicate_reference_rate=0.0,
            mean_parseable_line_rate=0.0,
            mean_description_present_rate=0.0,
            mean_description_budget_compliance_rate=0.0,
            mean_generic_description_rate=0.0,
            mean_discriminative_token_rate=0.0,
            mean_line_utilization=0.0,
            mean_byte_utilization=0.0,
            truncated_case_count=0,
            type_order_adherence_rate=0.0,
        )

    n_cases = len(metrics)
    ordered = [m.type_order_adherence for m in metrics if m.type_order_adherence is not None]
    return IndexOrganizationScorecard(
        n_cases=n_cases,
        mean_coverage_rate=sum(m.coverage_rate for m in metrics) / n_cases,
        mean_stale_reference_rate=sum(m.stale_reference_rate for m in metrics) / n_cases,
        mean_duplicate_reference_rate=sum(m.duplicate_reference_rate for m in metrics) / n_cases,
        mean_parseable_line_rate=sum(m.parseable_line_rate for m in metrics) / n_cases,
        mean_description_present_rate=sum(m.description_present_rate for m in metrics) / n_cases,
        mean_description_budget_compliance_rate=(
            sum(m.description_budget_compliance_rate for m in metrics) / n_cases
        ),
        mean_generic_description_rate=sum(m.generic_description_rate for m in metrics) / n_cases,
        mean_discriminative_token_rate=sum(m.discriminative_token_rate for m in metrics) / n_cases,
        mean_line_utilization=sum(m.line_utilization for m in metrics) / n_cases,
        mean_byte_utilization=sum(m.byte_utilization for m in metrics) / n_cases,
        truncated_case_count=sum(
            1 for m in metrics if m.was_line_truncated or m.was_byte_truncated
        ),
        type_order_adherence_rate=sum(ordered) / len(ordered) if ordered else 0.0,
    )


def _materialize_generated_case(
    case: GeneratedIndexEvalCase,
    memory_dir: Path,
) -> None:
    store = MemoryStore(memory_dir)
    for filename, content in case.memory_files.items():
        path = memory_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        parsed = parse_memory_file(path)
        if parsed is None:
            raise ValueError(f"{case.case_id}: invalid memory file {filename}")
        store.save_entry(parsed)


def _type_order_adherence(
    index_content: str,
    memory_dir: Path,
    expected_type_order: list[str] | None,
) -> float | None:
    if expected_type_order is None:
        return None
    observed: list[str] = []
    for entry in parse_memory_index(index_content):
        parsed = parse_memory_file(memory_dir / entry.filename)
        if parsed is not None:
            observed.append(parsed.memory_type.value)
    return 1.0 if observed == expected_type_order else 0.0


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
```

- [ ] **Step 4: Run generated index tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
git commit -m "Add generated memory index organization benchmark"
```

## Task 3: Add Description Quality Metrics And Raw Risk Cases

**Files:**
- Modify: `tests/memory/helpers/index_eval.py`
- Modify: `tests/memory/test_index_eval.py`

- [ ] **Step 1: Write failing description metric tests**

Append this to `tests/memory/test_index_eval.py`:

```python
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
            "pandas-style.md": _memory_file("user", "Pandas Style", "User prefers pandas dataframes"),
        },
        expected_present_filenames={"todo.md", "project-plan.md", "pandas-style.md"},
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
```

- [ ] **Step 2: Run description metric test to verify it fails if needed**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py::test_description_metrics_detect_generic_and_non_discriminative_entries -v
```

Expected: PASS if Task 1 already implemented description metrics exactly as planned. If it fails, update the helper to match the spec definitions.

- [ ] **Step 3: Add raw risk case helper**

Append this to `tests/memory/helpers/index_eval.py`:

```python
def raw_index_risk_cases() -> list[RawIndexEvalCase]:
    return [
        RawIndexEvalCase(
            case_id="raw-stale-reference",
            index_content=(
                "- [Existing](existing.md) - Existing memory\n"
                "- [Ghost](ghost.md) - Missing memory file\n"
            ),
            memory_files={
                "existing.md": _memory_file("user", "Existing", "Existing memory"),
            },
            expected_present_filenames={"existing.md"},
            risk_labels={"stale"},
        ),
        RawIndexEvalCase(
            case_id="raw-duplicate-reference",
            index_content=(
                "- [Existing](existing.md) - Existing memory\n"
                "- [Existing Again](existing.md) - Existing memory duplicate\n"
            ),
            memory_files={
                "existing.md": _memory_file("user", "Existing", "Existing memory"),
            },
            expected_present_filenames={"existing.md"},
            risk_labels={"duplicate"},
        ),
        RawIndexEvalCase(
            case_id="raw-generic-description",
            index_content="- [Todo](todo.md) - todo\n",
            memory_files={
                "todo.md": _memory_file("reference", "Todo", "todo"),
            },
            expected_present_filenames={"todo.md"},
            risk_labels={"generic-description"},
        ),
    ]
```

- [ ] **Step 4: Write and run raw risk case tests**

Append this to `tests/memory/test_index_eval.py`:

```python
from tests.memory.helpers.index_eval import raw_index_risk_cases


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
```

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
git commit -m "Add memory index description risk evaluation"
```

## Task 4: Add Budget Metrics And Compact Reporting

**Files:**
- Modify: `tests/memory/helpers/index_eval.py`
- Modify: `tests/memory/test_index_eval.py`

- [ ] **Step 1: Write failing budget and formatting tests**

Append this to `tests/memory/test_index_eval.py`:

```python
from tests.memory.helpers.index_eval import format_index_scorecard


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
```

Also add `MAX_ENTRYPOINT_BYTES` to the import list from `xxcode.memory.index` in `tests/memory/test_index_eval.py`:

```python
from xxcode.memory.index import MAX_ENTRYPOINT_BYTES
```

- [ ] **Step 2: Run budget and formatting tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py::test_budget_metrics_detect_truncation_risk tests/memory/test_index_eval.py::test_index_scorecard_summary_includes_key_metrics -v
```

Expected: FAIL because `format_index_scorecard` does not exist yet.

- [ ] **Step 3: Add compact scorecard formatting**

Append this to `tests/memory/helpers/index_eval.py`:

```python
def format_index_scorecard(scorecard: IndexOrganizationScorecard) -> str:
    return (
        "index "
        f"n_cases={scorecard.n_cases} "
        f"mean_coverage_rate={scorecard.mean_coverage_rate:.3f} "
        f"mean_stale_reference_rate={scorecard.mean_stale_reference_rate:.3f} "
        f"mean_duplicate_reference_rate={scorecard.mean_duplicate_reference_rate:.3f} "
        f"mean_parseable_line_rate={scorecard.mean_parseable_line_rate:.3f} "
        f"mean_description_present_rate={scorecard.mean_description_present_rate:.3f} "
        f"mean_generic_description_rate={scorecard.mean_generic_description_rate:.3f} "
        f"mean_discriminative_token_rate={scorecard.mean_discriminative_token_rate:.3f} "
        f"truncated_case_count={scorecard.truncated_case_count} "
        f"type_order_adherence_rate={scorecard.type_order_adherence_rate:.3f}"
    )
```

- [ ] **Step 4: Print scorecard summary from generated benchmark test**

In `test_generated_index_scorecard_reports_case_count`, after building `scorecard`, add:

```python
    print(format_index_scorecard(scorecard))
```

- [ ] **Step 5: Run index evaluation suite**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index_eval.py -v
```

Expected: PASS

- [ ] **Step 6: Run existing index and recall evaluation regression**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index.py tests/memory/test_index_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
git commit -m "Report deterministic memory index organization scorecards"
```

## Task 5: Final Verification

**Files:**
- Verify: `tests/memory/helpers/index_eval.py`
- Verify: `tests/memory/test_index_eval.py`
- Verify: existing memory tests

- [ ] **Step 1: Run focused index and recall evaluation suites**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_index.py tests/memory/test_index_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 2: Run full memory suite**

Run:

```powershell
py -3.11 -m pytest tests/memory -v
```

Expected: PASS

- [ ] **Step 3: Inspect changed files**

Run:

```powershell
git diff -- tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
```

Expected: diff only contains test-local index organization evaluation helpers and tests. There should be no production code changes for phase one.

- [ ] **Step 4: Final commit if previous task commits were batched instead**

If implementation was batched into one commit, run:

```bash
git add tests/memory/helpers/index_eval.py tests/memory/test_index_eval.py
git commit -m "Add deterministic memory index organization evaluation suite"
```
