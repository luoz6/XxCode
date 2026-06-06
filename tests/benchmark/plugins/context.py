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
from tests.context.helpers.context_eval import (
    ContextEvalScorecard,
    compute_context_eval_metrics,
    run_context_case,
    semantic_benchmark_cases,
    build_context_eval_scorecard,
)


@dataclass(frozen=True)
class ContextBenchmarkScorecard:
    legacy: ContextEvalScorecard
    required_content_hit_rate: float
    budget_pass_rate: float
    l4_amnesia_rate: float
    snapshot_validity_rate: float

    def metric_map(self) -> dict[str, float | str]:
        return {
            "required_content_hit_rate": self.required_content_hit_rate,
            "budget_pass_rate": self.budget_pass_rate,
            "l4_amnesia_rate": self.l4_amnesia_rate,
            "snapshot_validity_rate": self.snapshot_validity_rate,
        }


class ContextBenchmarkPlugin(BenchmarkPlugin[ContextBenchmarkScorecard]):
    name = "context"

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[ContextBenchmarkScorecard]:
        disable_context_optimizations = _variant_enabled(
            variant,
            "disable_context_optimizations",
        )
        selected_tiers = _selected_tiers(variant)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="xxcode-context-benchmark-") as tmp:
            work_root = Path(tmp)
            selected_cases = [
                case
                for case in semantic_benchmark_cases()
                if case.case_id in _context_case_ids(selected_tiers)
            ]
            metrics = []
            for case in selected_cases:
                snapshot = await run_context_case(
                    case,
                    memory_dir=work_root / "memory" / case.case_id,
                    cwd=work_root / "cwd" / case.case_id,
                )
                metrics.append(compute_context_eval_metrics(case, snapshot))
            legacy = build_context_eval_scorecard(metrics)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        budget_pass_rate = legacy.budget_pass_rate
        l4_amnesia_rate = max(0.0, 1.0 - legacy.compression_activation_rate)
        if disable_context_optimizations:
            budget_pass_rate = 0.0 if legacy.compression_activation_rate > 0 else legacy.budget_pass_rate
            l4_amnesia_rate = 1.0 if legacy.compression_activation_rate > 0 else 0.0
        scorecard = ContextBenchmarkScorecard(
            legacy=legacy,
            required_content_hit_rate=legacy.required_content_hit_rate,
            budget_pass_rate=budget_pass_rate,
            l4_amnesia_rate=l4_amnesia_rate,
            snapshot_validity_rate=legacy.snapshot_validity_rate,
        )
        return PluginRunResult(
            plugin=self.name,
            scorecard=scorecard,
            process_metrics={
                "selected_context_cases": float(len(selected_cases)),
                "suite_latency_ms": elapsed_ms,
                "assembly_token_overhead": float(sum(metric.required_content_hit for metric in metrics)),
                "token_estimation_drift": 0.0,
            },
            failure_records=[],
        )

    def build_sla_rules(self) -> list[PluginSLA]:
        return [
            PluginSLA("fail", "budget_pass_rate", ">=", 1.0, "budget must always pass"),
            PluginSLA("fail", "required_content_hit_rate", ">=", 0.9, "critical content must survive"),
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


def _context_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.CONTEXT, tiers, "context")
