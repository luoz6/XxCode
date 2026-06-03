# Memory Recall Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, CI-safe evaluation suite that reports memory recall quality and stability metrics for the recall stage.

**Architecture:** Keep phase one test-local. Add a focused helper module under `tests/memory/helpers/` that defines benchmark cases, materializes temporary memory directories, injects a deterministic selector client through the existing `client_factory`, computes metric scorecards, and generates controlled perturbations. Add one quality test module and one stability test module that exercise `recall_memories_for_query(...)` without changing production recall behavior.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, dataclasses, existing `xxcode.memory.recall` and `xxcode.memory.index` APIs

---

## File Structure

- Create: `tests/memory/helpers/__init__.py`
  Responsibility: make `tests.memory.helpers` importable without adding runtime package surface.
- Create: `tests/memory/helpers/recall_eval.py`
  Responsibility: case schema, case validation, temporary memory materialization, deterministic selector client, recall runner, quality metrics, stability perturbations, and scorecard formatting.
- Create: `tests/memory/test_recall_eval.py`
  Responsibility: quality benchmark tests, validation behavior tests, deterministic selector parsing tests, and quality scorecard threshold assertions.
- Create: `tests/memory/test_recall_stability.py`
  Responsibility: repeat, order, noise, and description robustness tests plus stability scorecard threshold assertions.
- Reuse without modification: `src/xxcode/memory/recall.py`
  Responsibility: production recall path under test, including prompt assembly, selector response parsing, valid-name filtering, and memory file loading.
- Reuse without modification: `src/xxcode/memory/index.py`
  Responsibility: `load_memory_index(...)`, `parse_memory_index(...)`, and truncation constants used by validation.

## Task 1: Add Recall Evaluation Helper Contract

**Files:**
- Create: `tests/memory/helpers/__init__.py`
- Create: `tests/memory/helpers/recall_eval.py`
- Create: `tests/memory/test_recall_eval.py`

- [ ] **Step 1: Write failing tests for case validation and deterministic selector parsing**

Create `tests/memory/test_recall_eval.py` with this initial content:

```python
import asyncio

import pytest

from tests.memory.helpers.recall_eval import (
    DeterministicRecallClient,
    RecallEvalCase,
    validate_case,
)


def test_validate_case_rejects_index_entry_without_memory_file():
    case = RecallEvalCase(
        case_id="ghost-index-entry",
        query="remember pandas preferences",
        index_content="- [Ghost](ghost.md) - User prefers pandas\n",
        memory_files={},
        expected_filenames={"ghost.md"},
        expected_top1="ghost.md",
    )

    with pytest.raises(ValueError, match="ghost.md"):
        validate_case(case)


def test_validate_case_rejects_expected_file_missing_from_index():
    case = RecallEvalCase(
        case_id="expected-not-indexed",
        query="remember pandas preferences",
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


def test_deterministic_selector_reads_available_memories_section():
    async def _run():
        client = DeterministicRecallClient()
        response = await client.complete(
            system_prompt="selector",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Query: pandas dataframe analysis\n\n"
                        "Available memories:\n"
                        "- [indexed] pandas-style.md: User prefers pandas dataframes\n"
                        "- [indexed] release-plan.md: Release deadline planning\n"
                    ),
                }
            ],
            max_tokens=256,
        )

        assert response == '["pandas-style.md"]'

    asyncio.run(_run())


def test_deterministic_selector_fails_when_manifest_section_missing():
    async def _run():
        client = DeterministicRecallClient()
        with pytest.raises(ValueError, match="Available memories"):
            await client.complete(
                system_prompt="selector",
                messages=[{"role": "user", "content": "Query: pandas"}],
                max_tokens=256,
            )

    asyncio.run(_run())
```

Create an empty package file:

```python
# tests/memory/helpers/__init__.py
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py -v
```

Expected: FAIL with `ModuleNotFoundError` because `tests.memory.helpers.recall_eval` does not exist.

- [ ] **Step 3: Add the minimal helper implementation**

Create `tests/memory/helpers/recall_eval.py` with:

```python
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from xxcode.memory.index import (
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    INDEX_FILENAME,
    parse_memory_index,
)
from xxcode.memory.recall import MAX_RECALLED_MEMORIES


_AVAILABLE_MEMORIES_HEADER = "Available memories:"
_INDEXED_MANIFEST_RE = re.compile(
    r"^- \[indexed\]\s+(?P<filename>[^\s:]+\.md):\s*(?P<description>.*)$"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RecallEvalCase:
    case_id: str
    query: str
    index_content: str
    memory_files: dict[str, str]
    expected_filenames: set[str]
    expected_top1: str | None = None


def validate_case(case: RecallEvalCase) -> None:
    index_bytes = len(case.index_content.encode("utf-8"))
    index_lines = len(case.index_content.rstrip("\n").splitlines())
    if index_lines > MAX_ENTRYPOINT_LINES:
        raise ValueError(
            f"{case.case_id}: index_content exceeds MAX_ENTRYPOINT_LINES"
        )
    if index_bytes > MAX_ENTRYPOINT_BYTES:
        raise ValueError(
            f"{case.case_id}: index_content exceeds MAX_ENTRYPOINT_BYTES"
        )

    indexed_filenames = {entry.filename for entry in parse_memory_index(case.index_content)}
    memory_filenames = set(case.memory_files)
    missing_memory_files = indexed_filenames - memory_filenames
    if missing_memory_files:
        missing = ", ".join(sorted(missing_memory_files))
        raise ValueError(
            f"{case.case_id}: index_content references files missing from "
            f"memory_files: {missing}"
        )

    missing_expected_files = case.expected_filenames - memory_filenames
    if missing_expected_files:
        missing = ", ".join(sorted(missing_expected_files))
        raise ValueError(
            f"{case.case_id}: expected filenames missing from memory_files: {missing}"
        )

    unindexed_expected_files = case.expected_filenames - indexed_filenames
    if unindexed_expected_files:
        missing = ", ".join(sorted(unindexed_expected_files))
        raise ValueError(
            f"{case.case_id}: expected filenames missing from index_content: {missing}"
        )

    if case.expected_top1 is not None and case.expected_top1 not in case.expected_filenames:
        raise ValueError(
            f"{case.case_id}: expected_top1 must be included in expected_filenames"
        )


def materialize_case(case: RecallEvalCase, memory_dir: Path) -> None:
    validate_case(case)
    memory_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in case.memory_files.items():
        (memory_dir / filename).write_text(content, encoding="utf-8")
    (memory_dir / INDEX_FILENAME).write_text(case.index_content, encoding="utf-8")


class DeterministicRecallClient:
    async def complete(
        self,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        del system_prompt, max_tokens, tools
        user_message = _first_user_text(messages or [])
        query = _extract_query(user_message)
        candidates = _extract_candidates(user_message)
        selected = _rank_candidates(query, candidates)
        return json.dumps(selected)


def _first_user_text(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                return content
    return ""


def _extract_query(user_message: str) -> str:
    for line in user_message.splitlines():
        if line.startswith("Query:"):
            return line.removeprefix("Query:").strip()
    return ""


def _extract_candidates(user_message: str) -> list[tuple[str, str]]:
    lines = user_message.splitlines()
    try:
        start = lines.index(_AVAILABLE_MEMORIES_HEADER) + 1
    except ValueError as exc:
        raise ValueError("Available memories section not found") from exc

    candidates: list[tuple[str, str]] = []
    for line in lines[start:]:
        if not line.strip():
            break
        match = _INDEXED_MANIFEST_RE.match(line.strip())
        if match:
            candidates.append((
                match.group("filename"),
                match.group("description"),
            ))
    return candidates


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _rank_candidates(query: str, candidates: list[tuple[str, str]]) -> list[str]:
    query_tokens = _tokens(query)
    ranked: list[tuple[int, str]] = []

    for filename, description in candidates:
        filename_tokens = _tokens(Path(filename).stem.replace("-", " "))
        description_tokens = _tokens(description)
        score = len(query_tokens & filename_tokens)
        score += 2 * len(query_tokens & description_tokens)
        if score > 0:
            ranked.append((-score, filename))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [filename for _score, filename in ranked[:MAX_RECALLED_MEMORIES]]
```

- [ ] **Step 4: Run the helper contract tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/__init__.py tests/memory/helpers/recall_eval.py tests/memory/test_recall_eval.py
git commit -m "Add deterministic recall evaluation helper"
```

## Task 2: Add Quality Metrics And Benchmark Cases

**Files:**
- Modify: `tests/memory/helpers/recall_eval.py`
- Modify: `tests/memory/test_recall_eval.py`

- [ ] **Step 1: Write failing quality metric and scorecard tests**

Append this to `tests/memory/test_recall_eval.py`:

```python
from tests.memory.helpers.recall_eval import (
    QualityMetrics,
    build_quality_scorecard,
    compute_quality_metrics,
    quality_benchmark_cases,
    run_recall_case,
)


def test_quality_metrics_compute_precision_recall_f1_and_top1():
    case = RecallEvalCase(
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
    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.mean_f1_at_k >= 0.95
    assert scorecard.top1_hit_rate >= 0.95
    assert scorecard.full_match_rate >= 0.95
```

- [ ] **Step 2: Run the quality tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py -v
```

Expected: FAIL because `QualityMetrics`, `run_recall_case`, `quality_benchmark_cases`, `compute_quality_metrics`, and `build_quality_scorecard` do not exist yet.

- [ ] **Step 3: Add quality metrics, scorecard, recall runner, and curated cases**

Update `tests/memory/helpers/recall_eval.py` by adding these imports near the top:

```python
from xxcode.memory.recall import recall_memories_for_query
```

Append this implementation to `tests/memory/helpers/recall_eval.py`:

```python
@dataclass(frozen=True)
class QualityMetrics:
    case_id: str
    selected_filenames: list[str]
    expected_filenames: set[str]
    precision_at_k: float
    recall_at_k: float
    f1_at_k: float
    top1_hit: float
    topk_full_match: float


@dataclass(frozen=True)
class QualityScorecard:
    n_cases: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    mean_f1_at_k: float
    top1_hit_rate: float
    full_match_rate: float


async def run_recall_case(case: RecallEvalCase, memory_dir: Path) -> list[str]:
    materialize_case(case, memory_dir)

    async def _client_factory():
        return DeterministicRecallClient()

    recalled = await recall_memories_for_query(
        query=case.query,
        memory_dir=memory_dir,
        client_factory=_client_factory,
    )
    return [memory.filename for memory in recalled]


def compute_quality_metrics(
    case: RecallEvalCase,
    selected_filenames: list[str],
) -> QualityMetrics:
    expected = set(case.expected_filenames)
    selected_set = set(selected_filenames)
    matched = len(selected_set & expected)

    precision = matched / len(selected_filenames) if selected_filenames else 0.0
    recall = matched / len(expected) if expected else 1.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    if case.expected_top1 is None:
        top1_hit = 1.0
    else:
        top1_hit = (
            1.0
            if selected_filenames and selected_filenames[0] == case.expected_top1
            else 0.0
        )

    return QualityMetrics(
        case_id=case.case_id,
        selected_filenames=selected_filenames,
        expected_filenames=expected,
        precision_at_k=precision,
        recall_at_k=recall,
        f1_at_k=f1,
        top1_hit=top1_hit,
        topk_full_match=1.0 if selected_set == expected else 0.0,
    )


def build_quality_scorecard(metrics: list[QualityMetrics]) -> QualityScorecard:
    if not metrics:
        return QualityScorecard(
            n_cases=0,
            mean_precision_at_k=0.0,
            mean_recall_at_k=0.0,
            mean_f1_at_k=0.0,
            top1_hit_rate=0.0,
            full_match_rate=0.0,
        )

    n_cases = len(metrics)
    return QualityScorecard(
        n_cases=n_cases,
        mean_precision_at_k=sum(m.precision_at_k for m in metrics) / n_cases,
        mean_recall_at_k=sum(m.recall_at_k for m in metrics) / n_cases,
        mean_f1_at_k=sum(m.f1_at_k for m in metrics) / n_cases,
        top1_hit_rate=sum(m.top1_hit for m in metrics) / n_cases,
        full_match_rate=sum(m.topk_full_match for m in metrics) / n_cases,
    )


def quality_benchmark_cases() -> list[RecallEvalCase]:
    return [
        RecallEvalCase(
            case_id="single-obvious-target",
            query="use pandas dataframe analysis",
            index_content=(
                "- [Pandas Style](pandas-style.md) - User prefers pandas "
                "dataframes for analysis\n"
                "- [Release Plan](release-plan.md) - Release deadline planning\n"
            ),
            memory_files={
                "pandas-style.md": _memory_file("user", "Pandas preference"),
                "release-plan.md": _memory_file("project", "Release plan"),
            },
            expected_filenames={"pandas-style.md"},
            expected_top1="pandas-style.md",
        ),
        RecallEvalCase(
            case_id="two-related-memories",
            query="prepare pytest memory recall quality regression tests",
            index_content=(
                "- [Recall Benchmark](recall-benchmark.md) - Memory recall "
                "quality benchmark uses pytest regression metrics\n"
                "- [Testing Style](testing-style.md) - Project prefers pytest "
                "red green refactor tests\n"
                "- [Deployment Note](deployment-note.md) - Production release "
                "checklist\n"
            ),
            memory_files={
                "recall-benchmark.md": _memory_file("project", "Recall benchmark"),
                "testing-style.md": _memory_file("feedback", "Testing style"),
                "deployment-note.md": _memory_file("reference", "Deployment"),
            },
            expected_filenames={"recall-benchmark.md", "testing-style.md"},
            expected_top1="recall-benchmark.md",
        ),
        RecallEvalCase(
            case_id="distractor-resistance",
            query="remember database backups warning",
            index_content=(
                "- [Migration Warning](migration-warning.md) - Database "
                "migration warning requires backups\n"
                "- [Migration Checklist](migration-checklist.md) - UI migration "
                "checklist for layout files\n"
                "- [Shell Reference](shell-reference.md) - Shell command usage "
                "reference\n"
            ),
            memory_files={
                "migration-warning.md": _memory_file("project", "Database warning"),
                "migration-checklist.md": _memory_file("reference", "UI checklist"),
                "shell-reference.md": _memory_file("reference", "Shell reference"),
            },
            expected_filenames={"migration-warning.md"},
            expected_top1="migration-warning.md",
        ),
        RecallEvalCase(
            case_id="description-beats-misleading-filename",
            query="handle backoff policy failures",
            index_content=(
                "- [Api Retry](api-retry.md) - Deprecated API naming note\n"
                "- [Retry Policy](retry-policy.md) - Retry backoff policy uses "
                "exponential backoff for API failures\n"
            ),
            memory_files={
                "api-retry.md": _memory_file("reference", "Deprecated API naming"),
                "retry-policy.md": _memory_file("project", "Retry backoff policy"),
            },
            expected_filenames={"retry-policy.md"},
            expected_top1="retry-policy.md",
        ),
        RecallEvalCase(
            case_id="generic-filename-relevant-description",
            query="recall metrics scorecard details",
            index_content=(
                "- [Note One](note-1.md) - Recall metrics scorecard includes "
                "precision recall f1 and top1 details\n"
                "- [Named Archive](named-archive.md) - Historical packaging "
                "archive\n"
            ),
            memory_files={
                "note-1.md": _memory_file("project", "Recall metric details"),
                "named-archive.md": _memory_file("reference", "Archive"),
            },
            expected_filenames={"note-1.md"},
            expected_top1="note-1.md",
        ),
        RecallEvalCase(
            case_id="cap-pressure",
            query="memory recall quality stability precision recall f1 top1 scorecard",
            index_content=(
                "- [Quality](quality.md) - Memory recall quality precision recall "
                "f1 metric\n"
                "- [Stability](stability.md) - Memory recall stability repeat "
                "order noise robustness\n"
                "- [Top One](top-one.md) - Top1 recall scorecard metric\n"
                "- [Full Match](full-match.md) - Full match recall scorecard "
                "metric\n"
                "- [Case Count](case-count.md) - Scorecard n cases reporting\n"
                "- [Weak Candidate](weak-candidate.md) - Memory note archive\n"
                "- [Other Candidate](other-candidate.md) - General project note\n"
            ),
            memory_files={
                "quality.md": _memory_file("project", "Quality metric"),
                "stability.md": _memory_file("project", "Stability metric"),
                "top-one.md": _memory_file("project", "Top1 metric"),
                "full-match.md": _memory_file("project", "Full match"),
                "case-count.md": _memory_file("project", "Case count"),
                "weak-candidate.md": _memory_file("reference", "Weak candidate"),
                "other-candidate.md": _memory_file("reference", "Other candidate"),
            },
            expected_filenames={
                "quality.md",
                "stability.md",
                "top-one.md",
                "full-match.md",
                "case-count.md",
            },
            expected_top1="quality.md",
        ),
    ]


def _memory_file(memory_type: str, body: str) -> str:
    return (
        "---\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{body}\n"
    )
```

- [ ] **Step 4: Run the quality tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/recall_eval.py tests/memory/test_recall_eval.py
git commit -m "Add deterministic memory recall quality benchmark"
```

## Task 3: Add Stability Perturbations And Scorecard

**Files:**
- Create: `tests/memory/test_recall_stability.py`
- Modify: `tests/memory/helpers/recall_eval.py`

- [ ] **Step 1: Write failing stability tests**

Create `tests/memory/test_recall_stability.py` with:

```python
import pytest

from tests.memory.helpers.recall_eval import (
    build_stability_scorecard,
    compute_stability_metrics,
    quality_benchmark_cases,
)


@pytest.mark.asyncio
async def test_repeat_consistency_uses_two_identical_runs(tmp_path):
    case = quality_benchmark_cases()[0]

    metrics = await compute_stability_metrics(case, tmp_path / case.case_id)

    assert metrics.repeat_run_count == 2
    assert metrics.repeat_consistency == 1.0


@pytest.mark.asyncio
async def test_generated_perturbations_preserve_expected_recall(tmp_path):
    case = quality_benchmark_cases()[1]

    metrics = await compute_stability_metrics(case, tmp_path / case.case_id)

    assert metrics.order_stability == 1.0
    assert metrics.description_robustness == 1.0
    assert metrics.noise_resistance == 1.0


@pytest.mark.asyncio
async def test_stability_scorecard_reports_case_count_and_rates(tmp_path):
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, tmp_path / case.case_id))

    scorecard = build_stability_scorecard(metrics)

    assert scorecard.n_cases == len(quality_benchmark_cases())
    assert scorecard.repeat_consistency_rate == 1.0
    assert scorecard.order_stability_rate == 1.0
    assert scorecard.description_robustness_rate == 1.0
    assert scorecard.noise_resistance_rate >= 0.95
```

- [ ] **Step 2: Run stability tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_stability.py -v
```

Expected: FAIL because `compute_stability_metrics` and `build_stability_scorecard` do not exist yet.

- [ ] **Step 3: Add stability metrics and perturbation helpers**

Append this to `tests/memory/helpers/recall_eval.py`:

```python
@dataclass(frozen=True)
class StabilityMetrics:
    case_id: str
    repeat_run_count: int
    repeat_consistency: float
    order_stability: float
    noise_resistance: float
    description_robustness: float
    baseline_filenames: list[str]


@dataclass(frozen=True)
class StabilityScorecard:
    n_cases: int
    repeat_consistency_rate: float
    order_stability_rate: float
    noise_resistance_rate: float
    description_robustness_rate: float


async def compute_stability_metrics(
    case: RecallEvalCase,
    base_dir: Path,
) -> StabilityMetrics:
    first = await run_recall_case(case, base_dir / "repeat-1")
    second = await run_recall_case(case, base_dir / "repeat-2")
    reordered = await run_recall_case(_with_reordered_index(case), base_dir / "order")
    noisy = await run_recall_case(_with_irrelevant_noise(case), base_dir / "noise")
    rewritten = await run_recall_case(
        _with_rewritten_non_target_descriptions(case),
        base_dir / "description",
    )

    baseline_set = set(first)
    return StabilityMetrics(
        case_id=case.case_id,
        repeat_run_count=2,
        repeat_consistency=1.0 if first == second else 0.0,
        order_stability=1.0 if baseline_set == set(reordered) else 0.0,
        noise_resistance=1.0 if baseline_set.issubset(set(noisy)) else 0.0,
        description_robustness=1.0 if baseline_set == set(rewritten) else 0.0,
        baseline_filenames=first,
    )


def build_stability_scorecard(
    metrics: list[StabilityMetrics],
) -> StabilityScorecard:
    if not metrics:
        return StabilityScorecard(
            n_cases=0,
            repeat_consistency_rate=0.0,
            order_stability_rate=0.0,
            noise_resistance_rate=0.0,
            description_robustness_rate=0.0,
        )

    n_cases = len(metrics)
    return StabilityScorecard(
        n_cases=n_cases,
        repeat_consistency_rate=sum(m.repeat_consistency for m in metrics) / n_cases,
        order_stability_rate=sum(m.order_stability for m in metrics) / n_cases,
        noise_resistance_rate=sum(m.noise_resistance for m in metrics) / n_cases,
        description_robustness_rate=(
            sum(m.description_robustness for m in metrics) / n_cases
        ),
    )


def _with_reordered_index(case: RecallEvalCase) -> RecallEvalCase:
    lines = [line for line in case.index_content.splitlines() if line.strip()]
    return RecallEvalCase(
        case_id=f"{case.case_id}:reordered",
        query=case.query,
        index_content="\n".join(reversed(lines)) + "\n",
        memory_files=dict(case.memory_files),
        expected_filenames=set(case.expected_filenames),
        expected_top1=case.expected_top1,
    )


def _with_irrelevant_noise(case: RecallEvalCase) -> RecallEvalCase:
    memory_files = dict(case.memory_files)
    memory_files.update({
        "noise-calendar.md": _memory_file("reference", "Calendar archive"),
        "noise-packaging.md": _memory_file("reference", "Packaging archive"),
        "noise-navigation.md": _memory_file("reference", "Navigation archive"),
    })
    noise_index = (
        "- [Noise Calendar](noise-calendar.md) - Calendar archive\n"
        "- [Noise Packaging](noise-packaging.md) - Packaging archive\n"
        "- [Noise Navigation](noise-navigation.md) - Navigation archive\n"
    )
    return RecallEvalCase(
        case_id=f"{case.case_id}:noise",
        query=case.query,
        index_content=case.index_content.rstrip() + "\n" + noise_index,
        memory_files=memory_files,
        expected_filenames=set(case.expected_filenames),
        expected_top1=case.expected_top1,
    )


def _with_rewritten_non_target_descriptions(case: RecallEvalCase) -> RecallEvalCase:
    rewritten_lines: list[str] = []
    for entry in parse_memory_index(case.index_content):
        if entry.filename in case.expected_filenames:
            rewritten_lines.append(
                f"- [{entry.title}]({entry.filename}) - {entry.description}"
            )
        else:
            rewritten_lines.append(
                f"- [{entry.title}]({entry.filename}) - Neutral unrelated archive note"
            )

    return RecallEvalCase(
        case_id=f"{case.case_id}:description",
        query=case.query,
        index_content="\n".join(rewritten_lines) + "\n",
        memory_files=dict(case.memory_files),
        expected_filenames=set(case.expected_filenames),
        expected_top1=case.expected_top1,
    )
```

- [ ] **Step 4: Run stability tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/memory/helpers/recall_eval.py tests/memory/test_recall_stability.py
git commit -m "Add deterministic memory recall stability benchmark"
```

## Task 4: Add Compact Scorecard Reporting And Full Regression Command

**Files:**
- Modify: `tests/memory/helpers/recall_eval.py`
- Modify: `tests/memory/test_recall_eval.py`
- Modify: `tests/memory/test_recall_stability.py`

- [ ] **Step 1: Write failing summary-format tests**

Append this to `tests/memory/test_recall_eval.py`:

```python
from tests.memory.helpers.recall_eval import format_quality_scorecard


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
    assert "mean_f1_at_k=1.000" in summary
    assert "full_match_rate=1.000" in summary
```

Append this to `tests/memory/test_recall_stability.py`:

```python
from tests.memory.helpers.recall_eval import format_stability_scorecard


def test_stability_scorecard_summary_includes_case_count_and_key_metrics():
    metrics = []
    scorecard = build_stability_scorecard(metrics)

    summary = format_stability_scorecard(scorecard)

    assert "n_cases=0" in summary
    assert "repeat_consistency_rate=0.000" in summary
    assert "noise_resistance_rate=0.000" in summary
```

- [ ] **Step 2: Run summary tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py::test_quality_scorecard_summary_includes_case_count_and_key_metrics tests/memory/test_recall_stability.py::test_stability_scorecard_summary_includes_case_count_and_key_metrics -v
```

Expected: FAIL because the formatting helpers do not exist yet.

- [ ] **Step 3: Add compact scorecard formatting helpers**

Append this to `tests/memory/helpers/recall_eval.py`:

```python
def format_quality_scorecard(scorecard: QualityScorecard) -> str:
    return (
        "quality "
        f"n_cases={scorecard.n_cases} "
        f"mean_precision_at_k={scorecard.mean_precision_at_k:.3f} "
        f"mean_recall_at_k={scorecard.mean_recall_at_k:.3f} "
        f"mean_f1_at_k={scorecard.mean_f1_at_k:.3f} "
        f"top1_hit_rate={scorecard.top1_hit_rate:.3f} "
        f"full_match_rate={scorecard.full_match_rate:.3f}"
    )


def format_stability_scorecard(scorecard: StabilityScorecard) -> str:
    return (
        "stability "
        f"n_cases={scorecard.n_cases} "
        f"repeat_consistency_rate={scorecard.repeat_consistency_rate:.3f} "
        f"order_stability_rate={scorecard.order_stability_rate:.3f} "
        f"noise_resistance_rate={scorecard.noise_resistance_rate:.3f} "
        f"description_robustness_rate={scorecard.description_robustness_rate:.3f}"
    )
```

- [ ] **Step 4: Print scorecard summaries from aggregate benchmark tests**

In `tests/memory/test_recall_eval.py`, update the aggregate quality test after building `scorecard`:

```python
    print(format_quality_scorecard(scorecard))
```

In `tests/memory/test_recall_stability.py`, update the aggregate stability test after building `scorecard`:

```python
    print(format_stability_scorecard(scorecard))
```

- [ ] **Step 5: Run the full recall evaluation suite**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 6: Run the existing recall tests as a regression check**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/memory/helpers/recall_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py
git commit -m "Report deterministic memory recall scorecards"
```

## Task 5: Final Plan Verification

**Files:**
- Verify: `tests/memory/helpers/recall_eval.py`
- Verify: `tests/memory/test_recall_eval.py`
- Verify: `tests/memory/test_recall_stability.py`

- [ ] **Step 1: Run the focused suite**

Run:

```powershell
py -3.11 -m pytest tests/memory/test_recall.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py -v
```

Expected: PASS

- [ ] **Step 2: Run the full memory suite**

Run:

```powershell
py -3.11 -m pytest tests/memory -v
```

Expected: PASS

- [ ] **Step 3: Inspect changed files**

Run:

```powershell
git diff -- tests/memory/helpers/__init__.py tests/memory/helpers/recall_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py
```

Expected: diff only contains test-local evaluation helpers and tests. There should be no production code changes for phase one.

- [ ] **Step 4: Final commit if previous task commits were batched instead**

If the implementation was done as one batch rather than task-by-task commits, commit the complete evaluation suite:

```bash
git add tests/memory/helpers/__init__.py tests/memory/helpers/recall_eval.py tests/memory/test_recall_eval.py tests/memory/test_recall_stability.py
git commit -m "Add deterministic memory recall evaluation suite"
```
