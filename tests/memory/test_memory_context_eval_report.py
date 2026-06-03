from dataclasses import replace

import pytest

from tests.context.helpers.context_eval import ContextEvalScorecard
from tests.context.helpers.context_eval import semantic_benchmark_cases
from tests.memory.helpers.effectiveness_eval import effectiveness_benchmark_cases
from tests.memory.helpers.effectiveness_eval import EffectivenessScorecard
from tests.memory.helpers.extraction_eval import extraction_quality_cases
from tests.memory.helpers.extraction_eval import ExtractionScorecard
from tests.memory.helpers.index_eval import generated_index_cases
from tests.memory.helpers.index_eval import IndexOrganizationScorecard
from tests.memory.helpers.recall_eval import (
    quality_benchmark_cases,
    QualityScorecard,
    StabilityScorecard,
)
from tests.memory.helpers.report_eval import (
    build_unified_report,
    format_unified_report,
    MemoryEvaluationSection,
    MetricCheck,
    UnifiedEvaluationReport,
    evaluate_thresholds,
    resolve_metric_value,
    validate_report,
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
