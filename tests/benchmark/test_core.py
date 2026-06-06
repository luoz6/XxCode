from __future__ import annotations

from dataclasses import dataclass

from xxcode.benchmark.core import BenchmarkCore, BenchmarkRun
from xxcode.benchmark.models import PluginRunResult, PluginSLA, VariantOverride
from xxcode.benchmark.registry import BenchmarkRegistry


@dataclass(frozen=True)
class _Scorecard:
    mean_f1_at_k: float
    memory_lift_rate: float = 0.0

    def metric_map(self) -> dict[str, float | str]:
        return {
            "mean_f1_at_k": self.mean_f1_at_k,
            "memory_lift_rate": self.memory_lift_rate,
        }


def test_compute_deltas_uses_scorecard_metric_map():
    core = BenchmarkCore()
    baseline = BenchmarkRun(
        variant=VariantOverride("baseline", "feature off"),
        plugin_results={
            "memory": PluginRunResult(
                plugin="memory",
                scorecard=_Scorecard(mean_f1_at_k=0.7, memory_lift_rate=0.0),
                process_metrics={},
                failure_records=[],
            )
        },
    )
    candidate = BenchmarkRun(
        variant=VariantOverride("candidate", "feature on"),
        plugin_results={
            "memory": PluginRunResult(
                plugin="memory",
                scorecard=_Scorecard(mean_f1_at_k=0.9, memory_lift_rate=1.0),
                process_metrics={},
                failure_records=[],
            )
        },
    )

    comparison = core.compute_deltas(baseline, candidate)

    assert round(comparison.metric_deltas["memory"]["mean_f1_at_k_delta"], 3) == 0.2
    assert comparison.metric_deltas["memory"]["memory_lift_rate_delta"] == 1.0


def test_core_evaluates_fail_and_warn_slas():
    core = BenchmarkCore()
    run = BenchmarkRun(
        variant=VariantOverride("candidate", "feature on"),
        plugin_results={
            "security": PluginRunResult(
                plugin="security",
                scorecard=_Scorecard(mean_f1_at_k=1.0),
                process_metrics={},
                failure_records=[],
            )
        },
    )

    verdict = core.evaluate(
        run,
        plugin_rules={
            "security": [
                PluginSLA("fail", "mean_f1_at_k", ">=", 0.9, "must stay high"),
                PluginSLA("warn", "memory_lift_rate", ">=", 0.5, "nice to have"),
            ]
        },
    )

    assert verdict.passed is True
    assert verdict.failure_records == []
    assert len(verdict.warnings) == 1


def test_plugin_build_sla_rules_returns_static_policy():
    class _Plugin:
        name = "memory"

        def build_sla_rules(self) -> list[PluginSLA]:
            return [
                PluginSLA("fail", "mean_f1_at_k", ">=", 0.9, "must stay high"),
                PluginSLA("warn", "memory_lift_rate", ">=", 0.5, "nice to have"),
            ]

    assert _Plugin().build_sla_rules()[0].metric_name == "mean_f1_at_k"


def test_registry_requires_protocol_compatible_plugins():
    registry = BenchmarkRegistry()
    assert registry.names() == []
