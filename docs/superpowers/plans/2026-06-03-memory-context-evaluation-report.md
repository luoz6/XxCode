# Memory And Context Evaluation Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic unified report that aggregates memory and context evaluation scorecards, applies explicit threshold checks, and renders a compact Chinese summary.

**Architecture:** Keep the report layer test-local under `XxCode/tests/memory/helpers/report_eval.py`. Reuse existing recall, index, extraction, effectiveness, and context helper APIs to build scorecards, then validate them, evaluate a fixed threshold registry, and return a structured `UnifiedEvaluationReport`. Cover the work with one dedicated pytest module that starts with synthetic scorecard unit tests and ends with async integration over the real benchmark corpus.

**Tech Stack:** Python 3.11, pytest, pytest-asyncio, dataclasses, pathlib, existing test-local evaluation helpers under `XxCode/tests/memory/helpers/` and `XxCode/tests/context/helpers/`

---

## File Structure

- Command working directory: run all commands from `F:\agent\XxCode`. The actual repository root is `F:\agent\XxCode\XxCode`, so file paths in this plan include the leading `XxCode/` directory. Git commands use the prefix `git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode`.
- Create: `XxCode/tests/memory/helpers/report_eval.py`
  Responsibility: unified report dataclasses, explicit threshold registry, scorecard validation, metric lookup, async benchmark aggregation, and Chinese formatting.
- Create: `XxCode/tests/memory/test_memory_context_eval_report.py`
  Responsibility: TDD coverage for metric checks, scorecard validation, threshold evaluation, async integration, and Chinese report rendering.
- Reuse without modification: `XxCode/tests/memory/helpers/recall_eval.py`
  Responsibility: recall benchmark cases, per-case metrics, and recall scorecards.
- Reuse without modification: `XxCode/tests/memory/helpers/index_eval.py`
  Responsibility: generated index benchmark cases and index scorecards.
- Reuse without modification: `XxCode/tests/memory/helpers/extraction_eval.py`
  Responsibility: extraction benchmark cases and extraction scorecards.
- Reuse without modification: `XxCode/tests/memory/helpers/effectiveness_eval.py`
  Responsibility: effectiveness benchmark cases and effectiveness scorecards.
- Reuse without modification: `XxCode/tests/context/helpers/context_eval.py`
  Responsibility: context semantic benchmark cases, snapshots, per-case metrics, and context scorecards.
- Unicode note: `report_eval.py` will intentionally contain Chinese string literals because the spec requires Chinese report output. Keep everything else ASCII unless the string is part of the rendered report.

## Task 1: Add Report Dataclasses And MetricCheck Evaluation

**Files:**
- Create: `XxCode/tests/memory/helpers/report_eval.py`
- Create: `XxCode/tests/memory/test_memory_context_eval_report.py`

- [ ] **Step 1: Write failing tests for `MetricCheck` and unified report properties**

Create `XxCode/tests/memory/test_memory_context_eval_report.py` with:

```python
from dataclasses import replace

import pytest

from tests.context.helpers.context_eval import ContextEvalScorecard
from tests.memory.helpers.effectiveness_eval import EffectivenessScorecard
from tests.memory.helpers.extraction_eval import ExtractionScorecard
from tests.memory.helpers.index_eval import IndexOrganizationScorecard
from tests.memory.helpers.recall_eval import QualityScorecard, StabilityScorecard
from tests.memory.helpers.report_eval import (
    MemoryEvaluationSection,
    MetricCheck,
    UnifiedEvaluationReport,
)


def _quality_scorecard() -> QualityScorecard:
    return QualityScorecard(
        n_cases=6,
        n_top1_cases=6,
        mean_precision_at_k=1.0,
        mean_recall_at_k=1.0,
        mean_f1_at_k=1.0,
        top1_hit_rate=1.0,
        full_match_rate=1.0,
    )


def _stability_scorecard() -> StabilityScorecard:
    return StabilityScorecard(
        n_cases=6,
        repeat_consistency_rate=1.0,
        order_stability_rate=1.0,
        noise_resistance_rate=1.0,
        description_robustness_rate=1.0,
    )


def _index_scorecard() -> IndexOrganizationScorecard:
    return IndexOrganizationScorecard(
        n_cases=2,
        mean_coverage_rate=1.0,
        mean_stale_reference_rate=0.0,
        mean_duplicate_reference_rate=0.0,
        mean_parseable_line_rate=1.0,
        mean_description_present_rate=1.0,
        mean_description_budget_compliance_rate=1.0,
        mean_generic_description_rate=0.0,
        mean_discriminative_token_rate=1.0,
        mean_line_utilization=0.5,
        mean_byte_utilization=0.5,
        truncated_case_count=0,
        type_order_adherence_rate=1.0,
    )


def _extraction_scorecard() -> ExtractionScorecard:
    return ExtractionScorecard(
        n_cases=7,
        mean_write_validity_rate=1.0,
        mean_field_completeness_rate=0.857,
        mean_expected_memory_coverage=1.0,
        mean_expected_fact_coverage=0.929,
        mean_grounding_rate=0.571,
        n_noise_cases=1,
        mean_noise_suppression_rate=1.0,
        total_forbidden_fact_leak_count=0,
        n_type_cases=6,
        mean_memory_type_accuracy=0.833,
        n_duplicate_cases=1,
        mean_duplicate_control_rate=0.0,
        n_conflict_cases=1,
        mean_conflict_update_correctness=0.0,
    )


def _effectiveness_scorecard() -> EffectivenessScorecard:
    return EffectivenessScorecard(
        n_cases=7,
        mean_answer_fact_coverage=0.571,
        n_memory_usage_cases=7,
        mean_memory_fact_usage_rate=0.571,
        n_preference_cases=4,
        mean_preference_adherence_rate=0.5,
        n_forbidden_cases=1,
        mean_forbidden_fact_absence_rate=0.0,
        n_obsolete_cases=1,
        mean_obsolete_fact_suppression_rate=0.0,
        n_lift_cases=2,
        memory_lift_rate=1.0,
        mean_memory_lift_delta=0.75,
    )


def _context_scorecard() -> ContextEvalScorecard:
    return ContextEvalScorecard(
        n_cases=3,
        required_content_hit_rate=1.0,
        required_order_pass_rate=1.0,
        section_presence_rate=1.0,
        recent_context_preservation_rate=1.0,
        stale_content_exclusion_rate=1.0,
        forbidden_content_absence_rate=1.0,
        budget_pass_rate=1.0,
        recall_activation_rate=1.0,
        compression_activation_rate=1.0,
        snapshot_validity_rate=1.0,
    )


def _memory_section() -> MemoryEvaluationSection:
    return MemoryEvaluationSection(
        recall_quality=_quality_scorecard(),
        recall_stability=_stability_scorecard(),
        index_organization=_index_scorecard(),
        extraction_quality=_extraction_scorecard(),
        effectiveness=_effectiveness_scorecard(),
    )


def _base_report() -> UnifiedEvaluationReport:
    return UnifiedEvaluationReport(
        memory=_memory_section(),
        context_engineering=_context_scorecard(),
        checks=[],
    )


def test_metric_check_evaluate_supports_expected_operators():
    assert MetricCheck.evaluate(
        "memory.recall_quality",
        "mean_f1_at_k",
        1.0,
        ">=",
        0.9,
    ).passed is True
    assert MetricCheck.evaluate(
        "memory.index",
        "mean_stale_reference_rate",
        0.0,
        "<=",
        0.0,
    ).passed is True
    assert MetricCheck.evaluate(
        "memory.recall_stability",
        "repeat_consistency_rate",
        1.0,
        "==",
        1.0,
    ).passed is True


def test_metric_check_rejects_unsupported_operator():
    with pytest.raises(ValueError, match="unsupported operator"):
        MetricCheck.evaluate(
            "memory.recall_quality",
            "mean_f1_at_k",
            1.0,
            "!=",
            0.9,
        )


def test_unified_report_exposes_passed_and_failed_checks():
    passing = MetricCheck.evaluate(
        "memory.recall_quality",
        "mean_f1_at_k",
        1.0,
        ">=",
        0.9,
    )
    failing = MetricCheck.evaluate(
        "memory.recall_quality",
        "full_match_rate",
        0.7,
        ">=",
        0.9,
    )
    report = replace(_base_report(), checks=[passing, failing])

    assert report.passed is False
    assert report.failed_checks == [failing]
```

- [ ] **Step 2: Run the Task 1 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: FAIL with `ModuleNotFoundError` because `tests.memory.helpers.report_eval` does not exist.

- [ ] **Step 3: Add minimal report helper dataclasses**

Create `XxCode/tests/memory/helpers/report_eval.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass

from tests.context.helpers.context_eval import ContextEvalScorecard
from tests.memory.helpers.effectiveness_eval import EffectivenessScorecard
from tests.memory.helpers.extraction_eval import ExtractionScorecard
from tests.memory.helpers.index_eval import IndexOrganizationScorecard
from tests.memory.helpers.recall_eval import QualityScorecard, StabilityScorecard


@dataclass(frozen=True)
class MetricCheck:
    section: str
    metric: str
    actual: float
    operator: str
    threshold: float
    passed: bool

    @classmethod
    def evaluate(
        cls,
        section: str,
        metric: str,
        actual: float,
        operator: str,
        threshold: float,
    ) -> "MetricCheck":
        if operator == ">=":
            passed = actual >= threshold
        elif operator == "<=":
            passed = actual <= threshold
        elif operator == "==":
            passed = actual == threshold
        else:
            raise ValueError(f"unsupported operator: {operator}")
        return cls(
            section=section,
            metric=metric,
            actual=actual,
            operator=operator,
            threshold=threshold,
            passed=passed,
        )


@dataclass(frozen=True)
class MemoryEvaluationSection:
    recall_quality: QualityScorecard
    recall_stability: StabilityScorecard
    index_organization: IndexOrganizationScorecard
    extraction_quality: ExtractionScorecard
    effectiveness: EffectivenessScorecard


@dataclass(frozen=True)
class UnifiedEvaluationReport:
    memory: MemoryEvaluationSection
    context_engineering: ContextEvalScorecard
    checks: list[MetricCheck]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[MetricCheck]:
        return [check for check in self.checks if not check.passed]
```

- [ ] **Step 4: Run the Task 1 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: PASS for the three new tests.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add tests/memory/helpers/report_eval.py tests/memory/test_memory_context_eval_report.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "test: add unified evaluation report core model"
```

## Task 2: Add Explicit Threshold Registry, Metric Lookup, And Empty-Section Validation

**Files:**
- Modify: `XxCode/tests/memory/helpers/report_eval.py`
- Modify: `XxCode/tests/memory/test_memory_context_eval_report.py`

- [ ] **Step 1: Extend tests for metric lookup, threshold evaluation, and zero-case validation**

Append to `XxCode/tests/memory/test_memory_context_eval_report.py`:

```python
from tests.memory.helpers.report_eval import (
    evaluate_thresholds,
    resolve_metric_value,
    validate_report,
)


def test_resolve_metric_value_reads_known_scorecard_fields():
    report = _base_report()

    assert resolve_metric_value(
        report,
        "memory.recall_quality",
        "mean_f1_at_k",
    ) == 1.0
    assert resolve_metric_value(
        report,
        "context.engineering",
        "budget_pass_rate",
    ) == 1.0


def test_resolve_metric_value_rejects_unknown_section():
    with pytest.raises(ValueError, match="unknown report section"):
        resolve_metric_value(
            _base_report(),
            "memory.unknown",
            "mean_f1_at_k",
        )


def test_resolve_metric_value_rejects_unknown_scorecard_field():
    with pytest.raises(ValueError, match="missing scorecard field"):
        resolve_metric_value(
            _base_report(),
            "memory.recall_quality",
            "not_a_metric",
        )


def test_validate_report_rejects_zero_case_sections():
    empty_quality = QualityScorecard(
        n_cases=0,
        n_top1_cases=0,
        mean_precision_at_k=0.0,
        mean_recall_at_k=0.0,
        mean_f1_at_k=0.0,
        top1_hit_rate=0.0,
        full_match_rate=0.0,
    )
    report = replace(
        _base_report(),
        memory=replace(_memory_section(), recall_quality=empty_quality),
    )

    with pytest.raises(ValueError, match="memory.recall_quality"):
        validate_report(report)


def test_evaluate_thresholds_uses_registry_and_reports_failures():
    report = _base_report()
    checks = evaluate_thresholds(report)

    assert len(checks) == 15
    assert all(check.passed for check in checks)

    degraded_quality = replace(_quality_scorecard(), mean_f1_at_k=0.7)
    degraded_report = replace(
        report,
        memory=replace(report.memory, recall_quality=degraded_quality),
    )
    degraded_checks = evaluate_thresholds(degraded_report)

    assert any(
        check.section == "memory.recall_quality"
        and check.metric == "mean_f1_at_k"
        and check.passed is False
        for check in degraded_checks
    )
```

- [ ] **Step 2: Run the Task 2 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: FAIL with `ImportError` because `resolve_metric_value`, `validate_report`, and `evaluate_thresholds` do not exist.

- [ ] **Step 3: Add threshold registry, scorecard lookup, and validation**

Update `XxCode/tests/memory/helpers/report_eval.py` by appending:

```python
_THRESHOLDS: list[tuple[str, str, str, float]] = [
    ("memory.recall_quality", "mean_f1_at_k", ">=", 0.90),
    ("memory.recall_quality", "full_match_rate", ">=", 0.90),
    ("memory.recall_stability", "repeat_consistency_rate", "==", 1.00),
    ("memory.recall_stability", "order_stability_rate", "==", 1.00),
    ("memory.recall_stability", "description_robustness_rate", "==", 1.00),
    ("memory.index", "mean_coverage_rate", ">=", 0.90),
    ("memory.index", "mean_stale_reference_rate", "<=", 0.00),
    ("memory.index", "mean_duplicate_reference_rate", "<=", 0.00),
    ("memory.extraction", "mean_write_validity_rate", ">=", 0.90),
    ("memory.extraction", "mean_expected_fact_coverage", ">=", 0.90),
    ("memory.effectiveness", "memory_lift_rate", ">=", 1.00),
    ("memory.effectiveness", "mean_memory_lift_delta", ">=", 0.50),
    ("context.engineering", "required_content_hit_rate", ">=", 0.90),
    ("context.engineering", "budget_pass_rate", ">=", 1.00),
    ("context.engineering", "snapshot_validity_rate", ">=", 1.00),
]


def resolve_metric_value(
    report: UnifiedEvaluationReport,
    section: str,
    metric: str,
) -> float:
    scorecard = _resolve_scorecard(report, section)
    if not hasattr(scorecard, metric):
        raise ValueError(
            f"{section}: missing scorecard field: {metric}"
        )
    value = getattr(scorecard, metric)
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"{section}.{metric}: expected numeric value, got {type(value).__name__}"
        )
    return float(value)


def validate_report(report: UnifiedEvaluationReport) -> None:
    for section, scorecard in _iter_section_scorecards(report):
        n_cases = getattr(scorecard, "n_cases", None)
        if n_cases == 0:
            raise ValueError(f"{section}: scorecard has no benchmark cases")


def evaluate_thresholds(report: UnifiedEvaluationReport) -> list[MetricCheck]:
    checks: list[MetricCheck] = []
    for section, metric, operator, threshold in _THRESHOLDS:
        actual = resolve_metric_value(report, section, metric)
        checks.append(
            MetricCheck.evaluate(
                section=section,
                metric=metric,
                actual=actual,
                operator=operator,
                threshold=threshold,
            )
        )
    return checks


def _iter_section_scorecards(
    report: UnifiedEvaluationReport,
) -> list[tuple[str, object]]:
    return [
        ("memory.recall_quality", report.memory.recall_quality),
        ("memory.recall_stability", report.memory.recall_stability),
        ("memory.index", report.memory.index_organization),
        ("memory.extraction", report.memory.extraction_quality),
        ("memory.effectiveness", report.memory.effectiveness),
        ("context.engineering", report.context_engineering),
    ]


def _resolve_scorecard(
    report: UnifiedEvaluationReport,
    section: str,
) -> object:
    for candidate_section, scorecard in _iter_section_scorecards(report):
        if candidate_section == section:
            return scorecard
    raise ValueError(f"unknown report section: {section}")
```

- [ ] **Step 4: Run the Task 2 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: PASS for the eight tests in the report module.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add tests/memory/helpers/report_eval.py tests/memory/test_memory_context_eval_report.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "test: add unified evaluation threshold checks"
```

## Task 3: Add Async Benchmark Aggregation Across Memory And Context

**Files:**
- Modify: `XxCode/tests/memory/helpers/report_eval.py`
- Modify: `XxCode/tests/memory/test_memory_context_eval_report.py`

- [ ] **Step 1: Write failing async integration tests for real benchmark aggregation**

Append to `XxCode/tests/memory/test_memory_context_eval_report.py`:

```python
from tests.context.helpers.context_eval import semantic_benchmark_cases
from tests.memory.helpers.effectiveness_eval import effectiveness_benchmark_cases
from tests.memory.helpers.extraction_eval import extraction_quality_cases
from tests.memory.helpers.index_eval import generated_index_cases
from tests.memory.helpers.recall_eval import quality_benchmark_cases
from tests.memory.helpers.report_eval import build_unified_report


@pytest.mark.asyncio
async def test_build_unified_report_aggregates_real_benchmarks(tmp_path):
    report = await build_unified_report(tmp_path)

    assert report.memory.recall_quality.n_cases == len(quality_benchmark_cases())
    assert report.memory.recall_stability.n_cases == len(quality_benchmark_cases())
    assert report.memory.index_organization.n_cases == len(generated_index_cases())
    assert report.memory.extraction_quality.n_cases == len(extraction_quality_cases())
    assert report.memory.effectiveness.n_cases == len(effectiveness_benchmark_cases())
    assert report.context_engineering.n_cases == len(semantic_benchmark_cases())
    assert len(report.checks) == 15
    assert report.passed is True
    assert report.failed_checks == []


@pytest.mark.asyncio
async def test_build_unified_report_creates_expected_work_directories(tmp_path):
    custom_context_cwd = tmp_path / "custom-context-cwd"

    await build_unified_report(tmp_path, context_cwd=custom_context_cwd)

    assert (tmp_path / "recall-quality").is_dir()
    assert (tmp_path / "recall-stability").is_dir()
    assert (tmp_path / "index").is_dir()
    assert (tmp_path / "context-memory").is_dir()
    assert custom_context_cwd.is_dir()
```

- [ ] **Step 2: Run the Task 3 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: FAIL with `ImportError` because `build_unified_report` does not exist.

- [ ] **Step 3: Add async scorecard builders and unified aggregation**

Update `XxCode/tests/memory/helpers/report_eval.py` imports to include:

```python
from pathlib import Path

from tests.context.helpers.context_eval import (
    build_context_eval_scorecard,
    compute_context_eval_metrics,
    run_context_case,
    semantic_benchmark_cases,
)
from tests.memory.helpers.effectiveness_eval import (
    EffectivenessScorecard,
    build_effectiveness_scorecard,
    compute_effectiveness_metrics,
    effectiveness_benchmark_cases,
)
from tests.memory.helpers.extraction_eval import (
    ExtractionScorecard,
    build_extraction_scorecard,
    compute_extraction_metrics,
    extraction_quality_cases,
)
from tests.memory.helpers.index_eval import (
    IndexOrganizationScorecard,
    build_index_scorecard,
    compute_generated_index_metrics,
    generated_index_cases,
)
from tests.memory.helpers.recall_eval import (
    QualityScorecard,
    StabilityScorecard,
    build_quality_scorecard,
    build_stability_scorecard,
    compute_quality_metrics,
    compute_stability_metrics,
    quality_benchmark_cases,
    run_recall_case,
)
```

Then append to `XxCode/tests/memory/helpers/report_eval.py`:

```python
async def build_unified_report(
    work_dir: Path,
    *,
    context_cwd: Path | None = None,
) -> UnifiedEvaluationReport:
    recall_quality = await _build_recall_quality_scorecard(work_dir / "recall-quality")
    recall_stability = await _build_recall_stability_scorecard(work_dir / "recall-stability")
    index_organization = _build_generated_index_scorecard(work_dir / "index")
    extraction_quality = _build_extraction_scorecard()
    effectiveness = _build_effectiveness_scorecard()
    context_engineering = await _build_context_engineering_scorecard(
        work_dir / "context-memory",
        context_cwd=context_cwd or work_dir / "context-cwd",
    )

    memory = MemoryEvaluationSection(
        recall_quality=recall_quality,
        recall_stability=recall_stability,
        index_organization=index_organization,
        extraction_quality=extraction_quality,
        effectiveness=effectiveness,
    )
    report = UnifiedEvaluationReport(
        memory=memory,
        context_engineering=context_engineering,
        checks=[],
    )
    validate_report(report)
    checks = evaluate_thresholds(report)
    return UnifiedEvaluationReport(
        memory=report.memory,
        context_engineering=report.context_engineering,
        checks=checks,
    )


async def _build_recall_quality_scorecard(work_dir: Path) -> QualityScorecard:
    metrics = []
    for case in quality_benchmark_cases():
        selected = await run_recall_case(case, work_dir / case.case_id)
        metrics.append(compute_quality_metrics(case, selected))
    return build_quality_scorecard(metrics)


async def _build_recall_stability_scorecard(work_dir: Path) -> StabilityScorecard:
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, work_dir / case.case_id))
    return build_stability_scorecard(metrics)


def _build_generated_index_scorecard(work_dir: Path) -> IndexOrganizationScorecard:
    metrics = [
        compute_generated_index_metrics(case, work_dir / case.case_id)
        for case in generated_index_cases()
    ]
    return build_index_scorecard(metrics)


def _build_extraction_scorecard() -> ExtractionScorecard:
    metrics = [
        compute_extraction_metrics(case)
        for case in extraction_quality_cases()
    ]
    return build_extraction_scorecard(metrics)


def _build_effectiveness_scorecard() -> EffectivenessScorecard:
    metrics = [
        compute_effectiveness_metrics(case)
        for case in effectiveness_benchmark_cases()
    ]
    return build_effectiveness_scorecard(metrics)


async def _build_context_engineering_scorecard(
    memory_root: Path,
    *,
    context_cwd: Path,
) -> ContextEvalScorecard:
    metrics = []
    for case in semantic_benchmark_cases():
        snapshot = await run_context_case(
            case,
            memory_dir=memory_root / case.case_id,
            cwd=context_cwd / case.case_id,
        )
        metrics.append(compute_context_eval_metrics(case, snapshot))
    return build_context_eval_scorecard(metrics)
```

- [ ] **Step 4: Run the Task 3 tests to verify they pass**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: PASS for the ten report tests, including the two async integration tests.

- [ ] **Step 5: Commit Task 3**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add tests/memory/helpers/report_eval.py tests/memory/test_memory_context_eval_report.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "feat: aggregate unified memory context scorecards"
```

## Task 4: Add Chinese Rendering And Full Regression Coverage

**Files:**
- Modify: `XxCode/tests/memory/helpers/report_eval.py`
- Modify: `XxCode/tests/memory/test_memory_context_eval_report.py`

- [ ] **Step 1: Write failing tests for Chinese rendering**

Append to `XxCode/tests/memory/test_memory_context_eval_report.py`:

```python
from tests.memory.helpers.report_eval import format_unified_report


def test_format_unified_report_uses_chinese_section_labels():
    report = replace(
        _base_report(),
        checks=evaluate_thresholds(_base_report()),
    )

    summary = format_unified_report(report)

    assert "统一评测报告 通过=True 失败项=0" in summary
    assert "记忆.召回质量 n_cases=6 mean_f1_at_k=1.000 full_match_rate=1.000" in summary
    assert "记忆.索引组织 n_cases=2 mean_coverage_rate=1.000 mean_stale_reference_rate=0.000" in summary
    assert "上下文.工程质量 n_cases=3 required_content_hit_rate=1.000 budget_pass_rate=1.000" in summary
    assert "阈值检查 passed=15 total=15" in summary


def test_format_unified_report_lists_failed_checks():
    failed_check = MetricCheck.evaluate(
        "memory.recall_quality",
        "mean_f1_at_k",
        0.7,
        ">=",
        0.9,
    )
    report = replace(_base_report(), checks=[failed_check])

    summary = format_unified_report(report)

    assert "统一评测报告 通过=False 失败项=1" in summary
    assert "阈值检查 passed=0 total=1" in summary
    assert (
        "失败 memory.recall_quality.mean_f1_at_k actual=0.700 expected >= 0.900"
        in summary
    )
```

- [ ] **Step 2: Run the Task 4 tests to verify they fail**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py -v
```

Expected: FAIL with `ImportError` because `format_unified_report` does not exist.

- [ ] **Step 3: Add Chinese formatter**

Append to `XxCode/tests/memory/helpers/report_eval.py`:

```python
def format_unified_report(report: UnifiedEvaluationReport) -> str:
    passed_checks = sum(1 for check in report.checks if check.passed)
    lines = [
        f"统一评测报告 通过={report.passed} 失败项={len(report.failed_checks)}",
        (
            "记忆.召回质量 "
            f"n_cases={report.memory.recall_quality.n_cases} "
            f"mean_f1_at_k={report.memory.recall_quality.mean_f1_at_k:.3f} "
            f"full_match_rate={report.memory.recall_quality.full_match_rate:.3f}"
        ),
        (
            "记忆.召回稳定性 "
            f"n_cases={report.memory.recall_stability.n_cases} "
            f"repeat_consistency_rate={report.memory.recall_stability.repeat_consistency_rate:.3f} "
            f"order_stability_rate={report.memory.recall_stability.order_stability_rate:.3f}"
        ),
        (
            "记忆.索引组织 "
            f"n_cases={report.memory.index_organization.n_cases} "
            f"mean_coverage_rate={report.memory.index_organization.mean_coverage_rate:.3f} "
            f"mean_stale_reference_rate={report.memory.index_organization.mean_stale_reference_rate:.3f}"
        ),
        (
            "记忆.提取质量 "
            f"n_cases={report.memory.extraction_quality.n_cases} "
            f"mean_write_validity_rate={report.memory.extraction_quality.mean_write_validity_rate:.3f} "
            f"mean_expected_fact_coverage={report.memory.extraction_quality.mean_expected_fact_coverage:.3f}"
        ),
        (
            "记忆.端到端有效性 "
            f"n_cases={report.memory.effectiveness.n_cases} "
            f"mean_answer_fact_coverage={report.memory.effectiveness.mean_answer_fact_coverage:.3f} "
            f"memory_lift_rate={report.memory.effectiveness.memory_lift_rate:.3f}"
        ),
        (
            "上下文.工程质量 "
            f"n_cases={report.context_engineering.n_cases} "
            f"required_content_hit_rate={report.context_engineering.required_content_hit_rate:.3f} "
            f"budget_pass_rate={report.context_engineering.budget_pass_rate:.3f}"
        ),
        f"阈值检查 passed={passed_checks} total={len(report.checks)}",
    ]
    for check in report.failed_checks:
        lines.append(
            "失败 "
            f"{check.section}.{check.metric} "
            f"actual={check.actual:.3f} expected {check.operator} {check.threshold:.3f}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run report tests and full regression**

Run:

```powershell
py -3.11 -m pytest XxCode/tests/memory/test_memory_context_eval_report.py XxCode/tests/memory/test_recall_eval.py XxCode/tests/memory/test_recall_stability.py XxCode/tests/memory/test_index_eval.py XxCode/tests/memory/test_extraction_eval.py XxCode/tests/memory/test_effectiveness_eval.py XxCode/tests/context/test_context_engineering_eval.py XxCode/tests/context/test_context_engineering_stability.py -v
```

Expected: PASS. The unified report tests pass, and the existing memory/context evaluation suites remain green.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode add tests/memory/helpers/report_eval.py tests/memory/test_memory_context_eval_report.py
git -c safe.directory=F:/agent/XxCode/XxCode -C XxCode commit -m "feat: format unified evaluation report"
```

## Self-Review Checklist

- Spec coverage:
  - async `Path`-accepting builder: covered in Task 3
  - explicit threshold registry and metric lookup: covered in Task 2
  - `n_cases == 0` validation: covered in Task 2
  - context semantic-only aggregation: covered in Task 3 through `semantic_benchmark_cases()`
  - Chinese rendering: covered in Task 4
  - real scorecard integration across memory and context: covered in Task 3
- Placeholder scan:
  - no placeholder markers or vague “add validation” steps remain
  - every command is explicit and runnable from `F:\agent\XxCode`
- Type consistency:
  - `context_engineering` naming matches the spec
  - scorecard class names and field names match existing helper modules
  - unified builder name is consistently `build_unified_report`
