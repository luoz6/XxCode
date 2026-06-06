from __future__ import annotations

from dataclasses import dataclass

from xxcode.benchmark.core import BenchmarkCore, BenchmarkRun
from xxcode.benchmark.models import PluginRunResult, VariantOverride


@dataclass(frozen=True)
class _Scorecard:
    hit_rate: float
    latency_ms: float

    def metric_map(self) -> dict[str, float | str]:
        return {
            "hit_rate": self.hit_rate,
            "latency_ms": self.latency_ms,
        }


def test_compute_deltas_emits_records_for_numeric_metrics():
    core = BenchmarkCore()
    baseline = BenchmarkRun(
        variant=VariantOverride("baseline", "feature off"),
        plugin_results={
            "memory": PluginRunResult(
                plugin="memory",
                scorecard=_Scorecard(hit_rate=0.6, latency_ms=20.0),
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
                scorecard=_Scorecard(hit_rate=0.9, latency_ms=24.0),
                process_metrics={},
                failure_records=[],
            )
        },
    )

    comparison = core.compute_deltas(baseline, candidate)

    assert round(comparison.metric_deltas["memory"]["hit_rate_delta"], 3) == 0.3
    assert round(comparison.metric_deltas["memory"]["latency_ms_delta"], 3) == 4.0
    assert len(comparison.delta_records) == 2
    assert {record.metric_name for record in comparison.delta_records} == {
        "hit_rate",
        "latency_ms",
    }
