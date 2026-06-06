from __future__ import annotations

from dataclasses import dataclass

from xxcode.benchmark.core import BenchmarkCore, BenchmarkRun
from xxcode.benchmark.models import PluginRunResult, PluginSLA, VariantOverride
from xxcode.benchmark.reporting import format_benchmark_report


@dataclass(frozen=True)
class _Scorecard:
    value: float

    def metric_map(self) -> dict[str, float | str]:
        return {"value": self.value}


def test_build_benchmark_report_renders_expected_sections():
    core = BenchmarkCore()
    run = BenchmarkRun(
        variant=VariantOverride("candidate", "feature on"),
        plugin_results={
            "memory": PluginRunResult(
                plugin="memory",
                scorecard=_Scorecard(value=1.0),
                process_metrics={"latency_ms": 1.0},
                failure_records=[],
            ),
            "context": PluginRunResult(
                plugin="context",
                scorecard=_Scorecard(value=1.0),
                process_metrics={"latency_ms": 2.0},
                failure_records=[],
            ),
            "security": PluginRunResult(
                plugin="security",
                scorecard=_Scorecard(value=1.0),
                process_metrics={"latency_ms": 3.0},
                failure_records=[],
            ),
        },
    )
    plugin_rules = {
        "memory": [PluginSLA("fail", "value", ">=", 0.5, "memory must pass")],
        "context": [PluginSLA("fail", "value", ">=", 0.5, "context must pass")],
        "security": [PluginSLA("fail", "value", ">=", 0.5, "security must pass")],
    }
    verdict = core.evaluate(run, plugin_rules=plugin_rules)
    report = core.build_benchmark_report(
        comparison=None,
        verdict=verdict,
        run=run,
        plugin_rules=plugin_rules,
    )

    markdown = format_benchmark_report(report)

    assert "# 基准评测报告" in markdown
    assert "## 总览" in markdown
    assert "## memory" in markdown
    assert "## context" in markdown
    assert "## security" in markdown
    assert "## 备注" in markdown
    assert "### 核心评分卡" in markdown
    assert "### 过程指标" in markdown
    assert "### SLA 规则" in markdown
    assert "### 失败说明" in markdown
    assert "### 收益方向" in markdown
    assert "| 指标 | 值 |" in markdown
    assert "`value`" in markdown
    assert "memory must pass" in markdown
    assert "- 无" in markdown
    assert "- 无警告" in markdown


def test_build_benchmark_report_localizes_observed_prefix_in_failures_and_warnings():
    core = BenchmarkCore()
    run = BenchmarkRun(
        variant=VariantOverride("candidate", "feature on"),
        plugin_results={
            "memory": PluginRunResult(
                plugin="memory",
                scorecard=_Scorecard(value=0.1),
                process_metrics={},
                failure_records=[],
            ),
        },
    )
    plugin_rules = {
        "memory": [
            PluginSLA("fail", "value", ">=", 0.5, "memory must pass"),
            PluginSLA("warn", "value", ">=", 0.8, "memory should stay strong"),
        ]
    }
    verdict = core.evaluate(run, plugin_rules=plugin_rules)
    report = core.build_benchmark_report(
        comparison=None,
        verdict=verdict,
        run=run,
        plugin_rules=plugin_rules,
    )

    markdown = format_benchmark_report(report)

    assert "期望：memory must pass；实际：观测值=0.1" in markdown
    assert "- 警告 `memory.value`：观测值=0.1" in markdown
