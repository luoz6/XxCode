from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from xxcode.benchmark import BenchmarkPluginName
from xxcode.benchmark.models import (
    BenchmarkPlugin,
    PluginRunResult,
    PluginSLA,
    VariantOverride,
)
from tests.benchmark.catalogs import execution_case_ids_for
from tests.memory.helpers.effectiveness_eval import (
    build_effectiveness_scorecard,
    compute_effectiveness_metrics,
    effectiveness_benchmark_cases,
    EffectivenessScorecard,
)
from tests.memory.helpers.extraction_eval import (
    build_extraction_scorecard,
    compute_extraction_metrics,
    extraction_quality_cases,
    ExtractionScorecard,
)
from tests.memory.helpers.index_eval import (
    build_index_scorecard,
    compute_generated_index_metrics,
    generated_index_cases,
    IndexOrganizationScorecard,
)
from tests.memory.helpers.recall_eval import (
    build_quality_scorecard,
    build_stability_scorecard,
    compute_quality_metrics,
    compute_stability_metrics,
    quality_benchmark_cases,
    QualityScorecard,
    StabilityMetrics,
    StabilityScorecard,
    run_recall_case,
)


@dataclass(frozen=True)
class MemoryBenchmarkScorecard:
    quality: QualityScorecard
    stability: StabilityScorecard
    index_organization: IndexOrganizationScorecard
    extraction_quality: ExtractionScorecard
    effectiveness: EffectivenessScorecard
    recall_mean_f1_at_k: float
    recall_full_match_rate: float
    memory_lift_rate: float
    mean_memory_lift_delta: float

    def metric_map(self) -> dict[str, float | str]:
        return {
            "recall_mean_f1_at_k": self.recall_mean_f1_at_k,
            "recall_full_match_rate": self.recall_full_match_rate,
            "memory_lift_rate": self.memory_lift_rate,
            "mean_memory_lift_delta": self.mean_memory_lift_delta,
            "quality_mean_f1_at_k": self.quality.mean_f1_at_k,
            "stability_repeat_consistency_rate": self.stability.repeat_consistency_rate,
            "index_mean_coverage_rate": self.index_organization.mean_coverage_rate,
            "extraction_mean_write_validity_rate": self.extraction_quality.mean_write_validity_rate,
            "effectiveness_mean_answer_fact_coverage": self.effectiveness.mean_answer_fact_coverage,
        }


class MemoryBenchmarkPlugin(BenchmarkPlugin[MemoryBenchmarkScorecard]):
    name = "memory"

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[MemoryBenchmarkScorecard]:
        disable_recall = _variant_enabled(variant, "disable_memory_recall")
        disable_extraction = _variant_enabled(variant, "disable_memory_extraction")
        disable_index = _variant_enabled(variant, "disable_memory_index")
        disable_effectiveness = _variant_enabled(variant, "disable_memory_effectiveness")
        selected_tiers = _selected_tiers(variant)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="xxcode-memory-benchmark-") as tmp:
            work_root = Path(tmp)

            quality_cases = [
                case
                for case in quality_benchmark_cases()
                if case.case_id in _memory_recall_case_ids(selected_tiers)
            ]
            quality_metrics = []
            for case in quality_cases:
                selected = (
                    []
                    if disable_recall
                    else await run_recall_case(
                        case,
                        work_root / "recall-quality" / case.case_id,
                    )
                )
                quality_metrics.append(compute_quality_metrics(case, selected))
            quality = build_quality_scorecard(quality_metrics)

            stability_metrics = []
            for case in quality_cases:
                if disable_recall:
                    stability_metrics.append(
                        StabilityMetrics(
                            case_id=case.case_id,
                            repeat_run_count=2,
                            repeat_consistency=0.0,
                            order_stability=0.0,
                            noise_resistance=0.0,
                            description_robustness=0.0,
                            baseline_filenames=[],
                        )
                    )
                else:
                    stability_metrics.append(
                        await compute_stability_metrics(
                            case,
                            work_root / "recall-stability" / case.case_id,
                        )
                    )
            stability = build_stability_scorecard(stability_metrics)

            index_cases = [
                case
                for case in generated_index_cases()
                if case.case_id in _memory_index_case_ids(selected_tiers)
            ]
            index = build_index_scorecard(
                [
                    compute_generated_index_metrics(
                        case,
                        work_root / "index" / case.case_id,
                    )
                    for case in index_cases
                ]
            )
            if disable_index:
                index = IndexOrganizationScorecard(
                    n_cases=index.n_cases,
                    mean_coverage_rate=0.0,
                    mean_stale_reference_rate=index.mean_stale_reference_rate,
                    mean_duplicate_reference_rate=index.mean_duplicate_reference_rate,
                    mean_parseable_line_rate=index.mean_parseable_line_rate,
                    mean_description_present_rate=index.mean_description_present_rate,
                    mean_description_budget_compliance_rate=index.mean_description_budget_compliance_rate,
                    mean_generic_description_rate=index.mean_generic_description_rate,
                    mean_discriminative_token_rate=index.mean_discriminative_token_rate,
                    mean_line_utilization=index.mean_line_utilization,
                    mean_byte_utilization=index.mean_byte_utilization,
                    truncated_case_count=index.truncated_case_count,
                    type_order_adherence_rate=index.type_order_adherence_rate,
                )

            extraction_cases = [
                case
                for case in extraction_quality_cases()
                if case.case_id in _memory_extraction_case_ids(selected_tiers)
            ]
            extraction = build_extraction_scorecard(
                [
                    compute_extraction_metrics(case)
                    for case in extraction_cases
                ]
            )
            if disable_extraction:
                extraction = ExtractionScorecard(
                    n_cases=extraction.n_cases,
                    mean_write_validity_rate=0.0,
                    mean_field_completeness_rate=0.0,
                    mean_expected_memory_coverage=0.0,
                    mean_expected_fact_coverage=0.0,
                    mean_grounding_rate=0.0,
                    n_noise_cases=extraction.n_noise_cases,
                    mean_noise_suppression_rate=0.0,
                    total_forbidden_fact_leak_count=extraction.total_forbidden_fact_leak_count,
                    n_type_cases=extraction.n_type_cases,
                    mean_memory_type_accuracy=0.0,
                    n_duplicate_cases=extraction.n_duplicate_cases,
                    mean_duplicate_control_rate=0.0,
                    n_conflict_cases=extraction.n_conflict_cases,
                    mean_conflict_update_correctness=0.0,
                )

            effectiveness_cases = [
                case
                for case in effectiveness_benchmark_cases()
                if case.case_id in _memory_effectiveness_case_ids(selected_tiers)
            ]
            effectiveness = build_effectiveness_scorecard(
                [
                    compute_effectiveness_metrics(case)
                    for case in effectiveness_cases
                ]
            )
            if disable_effectiveness:
                effectiveness = EffectivenessScorecard(
                    n_cases=effectiveness.n_cases,
                    mean_answer_fact_coverage=0.0,
                    n_memory_usage_cases=effectiveness.n_memory_usage_cases,
                    mean_memory_fact_usage_rate=0.0,
                    n_preference_cases=effectiveness.n_preference_cases,
                    mean_preference_adherence_rate=0.0,
                    n_forbidden_cases=effectiveness.n_forbidden_cases,
                    mean_forbidden_fact_absence_rate=0.0,
                    n_obsolete_cases=effectiveness.n_obsolete_cases,
                    mean_obsolete_fact_suppression_rate=0.0,
                    n_lift_cases=effectiveness.n_lift_cases,
                    memory_lift_rate=0.0,
                    mean_memory_lift_delta=0.0,
                )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        scorecard = MemoryBenchmarkScorecard(
            quality=quality,
            stability=stability,
            index_organization=index,
            extraction_quality=extraction,
            effectiveness=effectiveness,
            recall_mean_f1_at_k=quality.mean_f1_at_k,
            recall_full_match_rate=quality.full_match_rate,
            memory_lift_rate=effectiveness.memory_lift_rate,
            mean_memory_lift_delta=effectiveness.mean_memory_lift_delta,
        )
        return PluginRunResult(
            plugin=self.name,
            scorecard=scorecard,
            process_metrics={
                "selected_recall_cases": float(len(quality_cases)),
                "selected_index_cases": float(len(index_cases)),
                "selected_extraction_cases": float(len(extraction_cases)),
                "selected_effectiveness_cases": float(len(effectiveness_cases)),
                "suite_latency_ms": elapsed_ms,
                "write_recall_latency_ms": elapsed_ms,
            },
            failure_records=[],
        )

    def build_sla_rules(self) -> list[PluginSLA]:
        return [
            PluginSLA("fail", "recall_mean_f1_at_k", ">=", 0.9, "recall quality must stay high"),
            PluginSLA("fail", "recall_full_match_rate", ">=", 0.9, "recall should fully match the benchmark"),
            PluginSLA("warn", "memory_lift_rate", ">=", 0.5, "memory should produce lift"),
        ]


def _variant_enabled(variant: VariantOverride | None, key: str) -> bool:
    if variant is None or variant.config_overrides is None:
        return False
    return bool(variant.config_overrides.get(key, False))


def _selected_tiers(variant: VariantOverride | None) -> tuple[str, ...]:
    if variant is None or variant.config_overrides is None:
        return ("smoke", "core", "stress")
    tiers = variant.config_overrides.get("tiers")
    if tiers is None:
        return ("smoke", "core", "stress")
    return tuple(str(tier) for tier in tiers)


def _memory_recall_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.MEMORY, tiers, "recall")


def _memory_index_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.MEMORY, tiers, "index")


def _memory_extraction_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.MEMORY, tiers, "extraction")


def _memory_effectiveness_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.MEMORY, tiers, "effectiveness")
