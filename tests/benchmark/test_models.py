from __future__ import annotations

from pathlib import Path

from xxcode.benchmark.models import (
    BenchmarkPlugin,
    BenchmarkScorecard,
    FailureRecord,
    PluginRunResult,
    PluginSLA,
    VariantOverride,
)


class _DummyScorecard:
    def metric_map(self) -> dict[str, float | str]:
        return {"mean_f1": 1.0}


class _DummyPlugin:
    name = "memory"

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[_DummyScorecard]:
        del variant
        return PluginRunResult(
            plugin="memory",
            scorecard=_DummyScorecard(),
            process_metrics={"latency_ms": 0.0},
            failure_records=[],
        )

    def build_sla_rules(self) -> list[PluginSLA]:
        return []


def test_contract_types_are_importable_and_protocol_compatible():
    plugin: BenchmarkPlugin[_DummyScorecard] = _DummyPlugin()
    assert plugin is not None
    assert isinstance(_DummyScorecard().metric_map()["mean_f1"], float)


def test_failure_record_has_trace_fields():
    record = FailureRecord(
        case_id="case-1",
        plugin="context",
        category="L4_Amnesia",
        severity="fail",
        metric_name="l4_amnesia_rate",
        expected_behavior="retain current task state",
        actual_behavior="summary dropped the file path",
    )
    assert record.trace_id is None
    assert record.replay_log_path is None


def test_scorecard_protocol_is_runtime_checkable():
    scorecard = _DummyScorecard()
    assert isinstance(scorecard, BenchmarkScorecard)
