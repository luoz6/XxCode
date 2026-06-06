from __future__ import annotations

from dataclasses import dataclass

import pytest

from xxcode.benchmark import build_benchmark_report
from xxcode.benchmark.models import PluginRunResult, PluginSLA, VariantOverride
from tests.benchmark.plugins.context import ContextBenchmarkPlugin
from tests.benchmark.plugins.memory import MemoryBenchmarkPlugin
from tests.benchmark.plugins.security import SecurityBenchmarkPlugin


@pytest.mark.asyncio
async def test_build_benchmark_report_returns_renderable_report():
    report = await build_benchmark_report(
        [
            MemoryBenchmarkPlugin(),
            ContextBenchmarkPlugin(),
            SecurityBenchmarkPlugin(),
        ]
    )

    assert report.markdown.startswith("# 基准评测报告")
    assert len(report.plugin_sections) == 3
    assert {section.plugin for section in report.plugin_sections} == {
        "memory",
        "context",
        "security",
    }
    assert report.passed in {True, False}


@dataclass(frozen=True)
class _VariantScorecard:
    hit_rate: float

    def metric_map(self) -> dict[str, float | str]:
        return {"hit_rate": self.hit_rate}


class _VariantPlugin:
    name = "memory"

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[_VariantScorecard]:
        hit_rate = 0.6 if variant and variant.name == "baseline" else 0.9
        return PluginRunResult(
            plugin=self.name,
            scorecard=_VariantScorecard(hit_rate=hit_rate),
            process_metrics={},
            failure_records=[],
        )

    def build_sla_rules(self) -> list[PluginSLA]:
        return [PluginSLA("fail", "hit_rate", ">=", 0.5, "hit rate must stay healthy")]


@pytest.mark.asyncio
async def test_build_benchmark_report_includes_baseline_deltas():
    report = await build_benchmark_report(
        [_VariantPlugin()],
        variant=VariantOverride("candidate", "feature on"),
        baseline_plugins=[_VariantPlugin()],
        baseline_variant=VariantOverride("baseline", "feature off"),
    )

    assert report.delta_records
    assert "基线变体: baseline" in report.markdown
    assert "- `hit_rate_delta=0.300`" in report.markdown


@pytest.mark.asyncio
async def test_build_benchmark_report_accepts_named_baseline_profile():
    report = await build_benchmark_report(
        [
            MemoryBenchmarkPlugin(),
            ContextBenchmarkPlugin(),
            SecurityBenchmarkPlugin(),
        ],
        baseline_plugins=[
            MemoryBenchmarkPlugin(),
            ContextBenchmarkPlugin(),
            SecurityBenchmarkPlugin(),
        ],
        baseline_profile="memory_off",
    )

    assert report.delta_records
    assert "基线变体: baseline" in report.markdown
    assert any(
        record.plugin == "memory" and record.metric_name == "recall_mean_f1_at_k"
        for record in report.delta_records
    )


@pytest.mark.asyncio
async def test_build_benchmark_report_filters_by_tier():
    report = await build_benchmark_report(
        [
            MemoryBenchmarkPlugin(),
            ContextBenchmarkPlugin(),
            SecurityBenchmarkPlugin(),
        ],
        tiers=["smoke"],
    )

    section_map = {section.plugin: section for section in report.plugin_sections}

    assert section_map["memory"].process_metrics["selected_recall_cases"] == 1.0
    assert section_map["memory"].process_metrics["selected_index_cases"] == 0.0
    assert section_map["memory"].process_metrics["selected_extraction_cases"] == 1.0
    assert section_map["memory"].process_metrics["selected_effectiveness_cases"] == 1.0
    assert section_map["context"].process_metrics["selected_context_cases"] == 3.0
    assert section_map["security"].process_metrics["selected_security_cases"] == 4.0
