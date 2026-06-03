# Memory And Context Evaluation Report Design

> Version 0.1 | 2026-06-03 | Scope: deterministic unified reporting for memory and context engineering evaluation scorecards

## 1. Objective

The repository now has deterministic evaluation layers for memory recall,
memory index organization, memory extraction quality, memory end-to-end
effectiveness, and context engineering. Each layer can produce its own
scorecard, but there is no single report that answers:

- what the current memory and context evaluation state is
- which evaluation layer regressed
- which metrics failed their threshold
- how many benchmark cases contributed to each metric group

This design adds a report-only aggregation layer. It does not redefine the
underlying metrics and does not introduce live model evaluation.

## 2. Current State

Existing memory evaluation helpers:

- `tests/memory/helpers/recall_eval.py`
- `tests/memory/helpers/index_eval.py`
- `tests/memory/helpers/extraction_eval.py`
- `tests/memory/helpers/effectiveness_eval.py`

Existing context evaluation helper:

- `tests/context/helpers/context_eval.py`

Existing scorecard types:

- `QualityScorecard`
- `StabilityScorecard`
- `IndexOrganizationScorecard`
- `ExtractionScorecard`
- `EffectivenessScorecard`
- `ContextEvalScorecard`

Existing tests already verify each layer independently. The missing piece is a
stable cross-layer report that collects those scorecards and exposes a compact
diagnostic summary.

## 3. Problem To Solve

Layer-specific scorecards are useful for local validation, but they are
fragmented. A developer changing memory or context code must inspect multiple
test outputs to understand the overall evaluation state.

The unified report should make regressions easier to diagnose without creating
a misleading blended score. The report should preserve the semantic boundaries
between recall, index organization, extraction, effectiveness, and context
engineering.

## 4. Design Goals

This design should:

1. aggregate all existing deterministic scorecards into one report object
2. include both memory and context engineering evaluation results
3. keep every scorecard in its own named section
4. expose pass/fail status per metric threshold
5. retain `n_cases` and optional metric denominators where the source scorecard
   provides them
6. render a stable summary string suitable for pytest output
7. avoid a single blended overall score
8. keep the implementation test-local for version 0.1

## 5. Non-Goals

This design does not:

- add live LLM evaluation
- change production memory or context runtime behavior
- replace existing scorecard helpers
- merge unrelated metrics into a single weighted total
- produce a dashboard, HTML report, or persisted artifact in version 0.1
- expand the benchmark corpus

## 6. Report Architecture

The unified report should have three layers:

1. scorecard collection
2. threshold evaluation
3. report rendering

Scorecard collection runs the existing benchmark case helpers and builds their
existing scorecards. Threshold evaluation compares selected metrics against
explicit per-metric floors or ceilings. Report rendering produces a stable text
summary and keeps the structured report available for tests.

The report layer should not know the implementation details of individual
benchmark cases. It should consume the same public helper functions that the
existing tests already use.

## 7. Report Shape

Version 0.1 should define a test-local report model under:

- `tests/memory/helpers/report_eval.py`

The real report builder must be async and must accept filesystem paths because
four source scorecards need temporary directories:

```python
async def build_unified_report(
    work_dir: Path,
    *,
    context_cwd: Path | None = None,
) -> UnifiedEvaluationReport
```

Path contract:

- `work_dir` is the report builder's temporary workspace
- recall quality cases use subdirectories under `work_dir / "recall-quality"`
- recall stability cases use subdirectories under `work_dir / "recall-stability"`
- generated index cases use subdirectories under `work_dir / "index"`
- context memory files use subdirectories under `work_dir / "context-memory"`
- `context_cwd` defaults to `work_dir / "context-cwd"` when not provided

Tests should call this builder with `tmp_path` from pytest and should use
`@pytest.mark.asyncio`.

Recommended dataclasses:

```python
@dataclass(frozen=True)
class MetricCheck:
    section: str
    metric: str
    actual: float
    operator: str
    threshold: float
    passed: bool


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
```

The semantic shape should remain:

- one memory section containing the five memory scorecards
- one `context_engineering` section containing the context engineering semantic
  scorecard
- one flat list of metric threshold checks

## 8. Scorecard Sources

The report should build scorecards by reusing existing deterministic benchmark
functions:

- recall quality from `quality_benchmark_cases()`
- recall stability also from `quality_benchmark_cases()`; stability deliberately
  reuses the same curated recall cases and applies perturbations through
  `compute_stability_metrics(case, base_dir)`
- index organization from `generated_index_cases()` for the primary generated
  index scorecard
- extraction quality from `extraction_quality_cases()`
- effectiveness from `effectiveness_benchmark_cases()`
- context engineering from `semantic_benchmark_cases()`

Context engineering has both `semantic_benchmark_cases()` and
`stability_benchmark_cases()`. Version 0.1 of the unified report uses only
`semantic_benchmark_cases()` and stores that scorecard in the
`context_engineering` field. Context stability should remain covered by its
dedicated tests until a deferred report version adds a separate
`context_stability` section.

## 9. Threshold Contract

The unified report should use explicit per-metric thresholds. Thresholds should
be conservative and deterministic.

Threshold checks should support:

- `>=` for quality/pass-rate floors
- `<=` for error-rate ceilings
- `==` only for metrics that must be exactly stable

Version 0.1 should use a small explicit threshold registry. Each registry entry
must map `(section, metric)` to a concrete scorecard field. The implementation
should not use open-ended `getattr(scorecard, metric)` lookups against arbitrary
metric names. A missing registry entry is a setup error and should raise
`ValueError`.

Recommended registry shape:

```python
THRESHOLDS = [
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
```

These thresholds intentionally do not require all risk-case detection metrics to
be `1.0`. Some existing extraction and effectiveness benchmarks include
prewritten risk cases whose purpose is to prove that the evaluator detects bad
outputs, not that the simulated output is healthy.

Raw scorecards remain available for detailed inspection even when a metric is
not part of the threshold registry.

## 10. Pass/Fail Semantics

The report-level pass status is derived from metric checks:

- `report.passed` is true when every `MetricCheck.passed` is true
- `report.failed_checks` contains checks where `passed` is false

The report should not fail because a scorecard contains optional denominator
fields with zero applicable cases, unless the threshold explicitly targets that
denominator.

If an underlying scorecard has `n_cases == 0`, the report builder should fail
loudly with `ValueError`. Empty benchmark sections are setup errors, not valid
passing reports.

## 11. Rendering Contract

The formatted report should be stable and human-readable.

The final report text should be Chinese because the primary review workflow for
this evaluation module is Chinese.

Recommended success format:

```text
统一评测报告 通过=True 失败项=0
记忆.召回质量 n_cases=6 mean_f1_at_k=1.000 full_match_rate=1.000
记忆.召回稳定性 n_cases=6 repeat_consistency_rate=1.000 order_stability_rate=1.000
记忆.索引组织 n_cases=2 mean_coverage_rate=1.000 mean_stale_reference_rate=0.000
记忆.提取质量 n_cases=7 mean_write_validity_rate=1.000 mean_expected_fact_coverage=0.929
记忆.端到端有效性 n_cases=7 mean_answer_fact_coverage=0.571 memory_lift_rate=1.000
上下文.工程质量 n_cases=3 required_content_hit_rate=1.000 budget_pass_rate=1.000
阈值检查 passed=15 total=15
```

The renderer should include:

- report pass status
- failed check count
- one line per scorecard section
- check summary count

If failures exist, the renderer should include compact failed check details:

```text
统一评测报告 通过=False 失败项=1
记忆.召回质量 n_cases=6 mean_f1_at_k=0.700 full_match_rate=1.000
记忆.召回稳定性 n_cases=6 repeat_consistency_rate=1.000 order_stability_rate=1.000
记忆.索引组织 n_cases=2 mean_coverage_rate=1.000 mean_stale_reference_rate=0.000
记忆.提取质量 n_cases=7 mean_write_validity_rate=1.000 mean_expected_fact_coverage=0.929
记忆.端到端有效性 n_cases=7 mean_answer_fact_coverage=0.571 memory_lift_rate=1.000
上下文.工程质量 n_cases=3 required_content_hit_rate=1.000 budget_pass_rate=1.000
阈值检查 passed=14 total=15
失败 memory.recall_quality.mean_f1_at_k actual=0.700 expected >= 0.900
```

The renderer should avoid multiline dumps of raw dataclasses because pytest
failure output becomes hard to scan.

## 12. Data Flow

For each report build:

1. receive `work_dir: Path` and optional `context_cwd: Path | None`
2. create deterministic subdirectories under `work_dir`
3. run memory recall quality benchmark cases with `run_recall_case(case, memory_dir)`
4. build `QualityScorecard`
5. run memory recall stability benchmark cases with
   `compute_stability_metrics(case, base_dir)`
6. build `StabilityScorecard`
7. run generated index organization benchmark cases with
   `compute_generated_index_metrics(case, memory_dir)`
8. build `IndexOrganizationScorecard`
9. run extraction benchmark cases with `compute_extraction_metrics(case)`
10. build `ExtractionScorecard`
11. run effectiveness benchmark cases with `compute_effectiveness_metrics(case)`
12. build `EffectivenessScorecard`
13. run context engineering semantic benchmark cases with
    `run_context_case(case, memory_dir=context_memory_dir, cwd=context_case_cwd)`
14. build `ContextEvalScorecard`
15. validate that no scorecard has `n_cases == 0`
16. evaluate threshold checks through the explicit metric registry
17. return `UnifiedEvaluationReport`
18. render a compact Chinese summary for pytest output

The report builder must be async in version 0.1 because recall quality, recall
stability, and context case execution are async.

## 13. Error Handling And Diagnostics

The report builder should fail loudly when:

- a required scorecard cannot be built
- a required benchmark section has zero cases
- a threshold references a metric that is absent from the explicit registry
- a registry entry points at a scorecard field that does not exist
- a threshold operator is unsupported

Failure messages should include:

- section name
- metric name when applicable
- actual value when applicable
- threshold expectation when applicable

This keeps setup errors separate from metric regressions.

## 14. Testing Strategy

Implementation should follow TDD.

Recommended order:

1. write a failing unit test for `MetricCheck`
2. implement threshold comparison
3. write a failing test for empty scorecard validation
4. implement scorecard validation
5. write a failing test for report shape using small synthetic scorecards
6. implement report dataclasses and builder helpers
7. write a failing async integration test that calls
   `build_unified_report(tmp_path)`
8. implement the real benchmark aggregation
9. write a failing Chinese renderer test
10. implement stable Chinese report formatting
11. run memory and context evaluation tests together

Tests should assert structured fields first and formatted strings second.

## 15. File Boundaries

Version 0.1 should add:

- `tests/memory/helpers/report_eval.py`
- `tests/memory/test_memory_context_eval_report.py`

The report helper may import from `tests/context/helpers/context_eval.py`. That
is acceptable because this is a test-local evaluation layer.

No production code should be modified for this phase.

## 16. Known Limitations

Version 0.1 is a deterministic report aggregator, not a full observability
system.

Known limitations:

- no persisted JSON artifact
- no trend tracking across commits
- no live-model or semantic benchmark integration
- no weighting model across memory and context metrics
- no dashboard or visualization

These limitations are intentional. The first version should create a reliable
single report entry point before adding reporting features.

## 17. Success Criteria

This design is successful when the repository gains a deterministic unified
report that can answer:

- what memory scorecards currently report
- what context engineering scorecard currently reports
- whether high-signal thresholds passed
- which metric failed if the unified report fails
- how many benchmark cases contributed to each reported section

The report should be readable directly from pytest output and should not require
manual inspection of every layer-specific test file.

## 18. Deferred Work

Deferred phases may add:

- JSON artifact output
- CLI command for local report generation
- trend comparison against a stored baseline
- live-model optional benchmark reporting outside CI
- dashboard or HTML summary
- separate context semantic and context stability report sections

These are deferred until the deterministic unified report is stable.
