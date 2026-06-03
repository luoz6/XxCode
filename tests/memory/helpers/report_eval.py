from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tests.context.helpers.context_eval import (
    build_context_eval_scorecard,
    compute_context_eval_metrics,
    ContextEvalScorecard,
    run_context_case,
    semantic_benchmark_cases,
)
from tests.memory.helpers.effectiveness_eval import (
    build_effectiveness_scorecard as build_effectiveness_suite_scorecard,
    compute_effectiveness_metrics,
    effectiveness_benchmark_cases,
    EffectivenessScorecard,
)
from tests.memory.helpers.extraction_eval import (
    build_extraction_scorecard as build_extraction_suite_scorecard,
    compute_extraction_metrics,
    extraction_quality_cases,
    ExtractionScorecard,
)
from tests.memory.helpers.index_eval import (
    build_index_scorecard as build_index_suite_scorecard,
    compute_generated_index_metrics,
    generated_index_cases,
    IndexOrganizationScorecard,
)
from tests.memory.helpers.recall_eval import (
    build_quality_scorecard as build_quality_suite_scorecard,
    build_stability_scorecard as build_stability_suite_scorecard,
    compute_quality_metrics,
    compute_stability_metrics,
    quality_benchmark_cases,
    QualityScorecard,
    run_recall_case,
    StabilityScorecard,
)


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

_SCORECARD_GETTERS: dict[str, Callable[[UnifiedEvaluationReport], object]] = {
    "memory.recall_quality": lambda report: report.memory.recall_quality,
    "memory.recall_stability": lambda report: report.memory.recall_stability,
    "memory.index": lambda report: report.memory.index_organization,
    "memory.extraction": lambda report: report.memory.extraction_quality,
    "memory.effectiveness": lambda report: report.memory.effectiveness,
    "context.engineering": lambda report: report.context_engineering,
}

_METRIC_GETTERS: dict[str, dict[str, Callable[[object], float]]] = {
    "memory.recall_quality": {
        "mean_f1_at_k": lambda scorecard: float(scorecard.mean_f1_at_k),
        "full_match_rate": lambda scorecard: float(scorecard.full_match_rate),
    },
    "memory.recall_stability": {
        "repeat_consistency_rate": lambda scorecard: float(
            scorecard.repeat_consistency_rate
        ),
        "order_stability_rate": lambda scorecard: float(
            scorecard.order_stability_rate
        ),
        "description_robustness_rate": lambda scorecard: float(
            scorecard.description_robustness_rate
        ),
    },
    "memory.index": {
        "mean_coverage_rate": lambda scorecard: float(scorecard.mean_coverage_rate),
        "mean_stale_reference_rate": lambda scorecard: float(
            scorecard.mean_stale_reference_rate
        ),
        "mean_duplicate_reference_rate": lambda scorecard: float(
            scorecard.mean_duplicate_reference_rate
        ),
    },
    "memory.extraction": {
        "mean_write_validity_rate": lambda scorecard: float(
            scorecard.mean_write_validity_rate
        ),
        "mean_expected_fact_coverage": lambda scorecard: float(
            scorecard.mean_expected_fact_coverage
        ),
    },
    "memory.effectiveness": {
        "memory_lift_rate": lambda scorecard: float(scorecard.memory_lift_rate),
        "mean_memory_lift_delta": lambda scorecard: float(
            scorecard.mean_memory_lift_delta
        ),
    },
    "context.engineering": {
        "required_content_hit_rate": lambda scorecard: float(
            scorecard.required_content_hit_rate
        ),
        "budget_pass_rate": lambda scorecard: float(scorecard.budget_pass_rate),
        "snapshot_validity_rate": lambda scorecard: float(
            scorecard.snapshot_validity_rate
        ),
    },
}


def resolve_metric_value(
    report: UnifiedEvaluationReport,
    section: str,
    metric: str,
) -> float:
    scorecard_getter = _SCORECARD_GETTERS.get(section)
    if scorecard_getter is None:
        raise ValueError(f"unknown report section: {section}")

    metric_getter = _METRIC_GETTERS.get(section, {}).get(metric)
    if metric_getter is None:
        raise ValueError(f"{section}: missing scorecard field: {metric}")

    return metric_getter(scorecard_getter(report))


def validate_report(report: UnifiedEvaluationReport) -> None:
    for section, scorecard_getter in _SCORECARD_GETTERS.items():
        scorecard = scorecard_getter(report)
        if getattr(scorecard, "n_cases", None) == 0:
            raise ValueError(f"{section}: scorecard has no benchmark cases")


def evaluate_thresholds(report: UnifiedEvaluationReport) -> list[MetricCheck]:
    checks: list[MetricCheck] = []
    for section, metric, operator, threshold in _THRESHOLDS:
        checks.append(
            MetricCheck.evaluate(
                section=section,
                metric=metric,
                actual=resolve_metric_value(report, section, metric),
                operator=operator,
                threshold=threshold,
            )
        )
    return checks


async def build_unified_report(
    work_dir: Path,
    *,
    context_cwd: Path | None = None,
) -> UnifiedEvaluationReport:
    report = UnifiedEvaluationReport(
        memory=MemoryEvaluationSection(
            recall_quality=await _build_recall_quality_scorecard(
                work_dir / "recall-quality"
            ),
            recall_stability=await _build_recall_stability_scorecard(
                work_dir / "recall-stability"
            ),
            index_organization=_build_generated_index_scorecard(work_dir / "index"),
            extraction_quality=_build_extraction_scorecard(),
            effectiveness=_build_effectiveness_scorecard(),
        ),
        context_engineering=await _build_context_engineering_scorecard(
            work_dir / "context-memory",
            context_cwd=context_cwd or work_dir / "context-cwd",
        ),
        checks=[],
    )
    validate_report(report)
    return UnifiedEvaluationReport(
        memory=report.memory,
        context_engineering=report.context_engineering,
        checks=evaluate_thresholds(report),
    )


async def _build_recall_quality_scorecard(work_dir: Path) -> QualityScorecard:
    metrics = []
    for case in quality_benchmark_cases():
        selected = await run_recall_case(case, work_dir / case.case_id)
        metrics.append(compute_quality_metrics(case, selected))
    return build_quality_suite_scorecard(metrics)


async def _build_recall_stability_scorecard(work_dir: Path) -> StabilityScorecard:
    metrics = []
    for case in quality_benchmark_cases():
        metrics.append(await compute_stability_metrics(case, work_dir / case.case_id))
    return build_stability_suite_scorecard(metrics)


def _build_generated_index_scorecard(work_dir: Path) -> IndexOrganizationScorecard:
    metrics = [
        compute_generated_index_metrics(case, work_dir / case.case_id)
        for case in generated_index_cases()
    ]
    return build_index_suite_scorecard(metrics)


def _build_extraction_scorecard() -> ExtractionScorecard:
    metrics = [compute_extraction_metrics(case) for case in extraction_quality_cases()]
    return build_extraction_suite_scorecard(metrics)


def _build_effectiveness_scorecard() -> EffectivenessScorecard:
    metrics = [
        compute_effectiveness_metrics(case) for case in effectiveness_benchmark_cases()
    ]
    return build_effectiveness_suite_scorecard(metrics)


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
