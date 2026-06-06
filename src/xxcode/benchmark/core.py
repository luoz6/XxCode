from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    BenchmarkPlugin,
    BenchmarkScorecard,
    FailureRecord,
    PluginRunResult,
    PluginSLA,
    VariantOverride,
)


@dataclass(frozen=True)
class BenchmarkRun:
    variant: VariantOverride
    plugin_results: dict[str, PluginRunResult[BenchmarkScorecard]]


@dataclass(frozen=True)
class MetricDelta:
    plugin: str
    metric_name: str
    baseline_value: float | str | None
    candidate_value: float | str | None
    delta: float | None


@dataclass(frozen=True)
class BenchmarkComparison:
    baseline: BenchmarkRun
    candidate: BenchmarkRun
    metric_deltas: dict[str, dict[str, float | str]]
    delta_records: list[MetricDelta]


@dataclass(frozen=True)
class BenchmarkVerdict:
    passed: bool
    failure_records: list[FailureRecord]
    warnings: list[FailureRecord]


@dataclass(frozen=True)
class PluginSection:
    plugin: str
    scorecard: BenchmarkScorecard
    process_metrics: dict[str, float]
    failure_records: list[FailureRecord]
    sla_rules: list[PluginSLA]
    benefit_direction: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BenchmarkReport:
    passed: bool
    markdown: str
    overview: list[str]
    plugin_sections: list[PluginSection]
    failure_records: list[FailureRecord]
    warnings: list[FailureRecord]
    delta_records: list[MetricDelta]
    notes: list[str]


_REPORT_LABELS = {
    "report_title": "基准评测报告",
    "overview": "总览",
    "core_scorecard": "核心评分卡",
    "process_metrics": "过程指标",
    "sla_rules": "SLA 规则",
    "failure_explanations": "失败说明",
    "benefit_direction": "收益方向",
    "notes": "备注",
    "metric_header": "指标",
    "value_header": "值",
    "empty_metric": "（无）",
    "not_applicable": "不适用",
    "empty": "无",
    "no_baseline_comparison": "无基线对比",
    "no_warnings": "无警告",
    "warning": "警告",
}

_OVERVIEW_LABELS = {
    "passed": "通过状态",
    "plugins": "插件数",
    "failures": "失败数",
    "warnings": "警告数",
    "variant": "评测变体",
    "baseline": "基线变体",
}


class BenchmarkCore:
    def build_run(
        self,
        variant: VariantOverride,
        plugin_results: dict[str, PluginRunResult[BenchmarkScorecard]],
    ) -> BenchmarkRun:
        return BenchmarkRun(variant=variant, plugin_results=plugin_results)

    def compute_deltas(
        self,
        baseline: BenchmarkRun,
        candidate: BenchmarkRun,
    ) -> BenchmarkComparison:
        delta_records: list[MetricDelta] = []
        metric_deltas: dict[str, dict[str, float | str]] = {}
        for plugin_name, candidate_result in candidate.plugin_results.items():
            baseline_result = baseline.plugin_results.get(plugin_name)
            if baseline_result is None:
                continue
            candidate_metrics = candidate_result.scorecard.metric_map()
            baseline_metrics = baseline_result.scorecard.metric_map()
            plugin_deltas: dict[str, float | str] = {}
            for metric_name, candidate_value in candidate_metrics.items():
                baseline_value = baseline_metrics.get(metric_name)
                delta_value = _delta_value(baseline_value, candidate_value)
                if delta_value is not None:
                    plugin_deltas[f"{metric_name}_delta"] = delta_value
                delta_records.append(
                    MetricDelta(
                        plugin=plugin_name,
                        metric_name=metric_name,
                        baseline_value=baseline_value,
                        candidate_value=candidate_value,
                        delta=delta_value,
                    )
                )
            metric_deltas[plugin_name] = plugin_deltas
        return BenchmarkComparison(
            baseline=baseline,
            candidate=candidate,
            metric_deltas=metric_deltas,
            delta_records=delta_records,
        )

    async def run_suite(
        self,
        plugin: BenchmarkPlugin[BenchmarkScorecard],
        variant: VariantOverride | None,
    ) -> PluginRunResult[BenchmarkScorecard]:
        return await plugin.run_suite(variant)

    def evaluate(
        self,
        run: BenchmarkRun,
        plugin_rules: dict[str, list[PluginSLA]] | None = None,
    ) -> BenchmarkVerdict:
        plugin_rules = plugin_rules or {}
        failures: list[FailureRecord] = []
        warnings: list[FailureRecord] = []
        passed = True
        for plugin_name, result in run.plugin_results.items():
            rules = plugin_rules.get(plugin_name, [])
            metrics = result.scorecard.metric_map()
            for sla in rules:
                value = metrics.get(sla.metric_name)
                if value is None:
                    continue
                ok = _satisfies(value, sla.operator, sla.threshold)
                if ok:
                    continue
                record = FailureRecord(
                    case_id="aggregate",
                    plugin=plugin_name,
                    category="sla",
                    severity=sla.severity,
                    metric_name=sla.metric_name,
                    expected_behavior=sla.reason,
                    actual_behavior=f"observed={value}",
                    observed_value=value,
                    expected_value=sla.threshold,
                )
                if sla.severity == "fail":
                    failures.append(record)
                    passed = False
                else:
                    warnings.append(record)
        return BenchmarkVerdict(passed=passed, failure_records=failures, warnings=warnings)

    def build_benchmark_report(
        self,
        comparison: BenchmarkComparison | None,
        verdict: BenchmarkVerdict,
        run: BenchmarkRun,
        *,
        plugin_rules: dict[str, list[PluginSLA]] | None = None,
    ) -> BenchmarkReport:
        plugin_rules = plugin_rules or {}
        overview = _build_overview_lines(verdict, run, comparison)
        plugin_sections: list[PluginSection] = []
        for plugin_name, result in run.plugin_results.items():
            delta_metrics = (
                {}
                if comparison is None
                else comparison.metric_deltas.get(plugin_name, {})
            )
            plugin_sections.append(
                PluginSection(
                    plugin=plugin_name,
                    scorecard=result.scorecard,
                    process_metrics=result.process_metrics,
                    failure_records=[
                        *result.failure_records,
                        *[
                            record
                            for record in verdict.failure_records
                            if record.plugin == plugin_name
                        ],
                    ],
                    sla_rules=plugin_rules.get(plugin_name, []),
                    benefit_direction=[
                        f"{metric}={_format_metric_value(value)}"
                        for metric, value in sorted(delta_metrics.items())
                    ],
                )
            )
        notes = _build_note_lines(verdict, comparison)
        markdown = _render_markdown(overview, plugin_sections, verdict, notes)
        return BenchmarkReport(
            passed=verdict.passed,
            markdown=markdown,
            overview=overview,
            plugin_sections=plugin_sections,
            failure_records=verdict.failure_records,
            warnings=verdict.warnings,
            delta_records=[] if comparison is None else comparison.delta_records,
            notes=notes,
        )


def _satisfies(value: float | str, operator: str, threshold: float) -> bool:
    if not isinstance(value, (int, float)):
        return True
    if operator == ">=":
        return float(value) >= threshold
    if operator == "<=":
        return float(value) <= threshold
    if operator == "==":
        return float(value) == threshold
    raise ValueError(f"unsupported operator: {operator}")


def _delta_value(baseline_value: float | str | None, candidate_value: float | str | None) -> float | None:
    if not isinstance(baseline_value, (int, float)) or not isinstance(candidate_value, (int, float)):
        return None
    return float(candidate_value) - float(baseline_value)


def _build_overview_lines(
    verdict: BenchmarkVerdict,
    run: BenchmarkRun,
    comparison: BenchmarkComparison | None,
) -> list[str]:
    lines = [
        f"{_OVERVIEW_LABELS['passed']}: {verdict.passed}",
        f"{_OVERVIEW_LABELS['plugins']}: {len(run.plugin_results)}",
        f"{_OVERVIEW_LABELS['failures']}: {len(verdict.failure_records)}",
        f"{_OVERVIEW_LABELS['warnings']}: {len(verdict.warnings)}",
        f"{_OVERVIEW_LABELS['variant']}: {run.variant.name}",
    ]
    if comparison is not None:
        lines.append(
            f"{_OVERVIEW_LABELS['baseline']}: {comparison.baseline.variant.name}"
        )
    return lines


def _build_note_lines(
    verdict: BenchmarkVerdict,
    comparison: BenchmarkComparison | None,
) -> list[str]:
    return [
        f"warnings={len(verdict.warnings)}",
        (
            "baseline_comparison=unavailable"
            if comparison is None
            else f"baseline_comparison=deltas:{len(comparison.delta_records)}"
        ),
    ]


def _normalize_actual_behavior(text: str) -> str:
    if text.startswith("observed="):
        return "观测值=" + text.removeprefix("observed=")
    return text


def _render_markdown(
    overview: list[str],
    plugin_sections: list[PluginSection],
    verdict: BenchmarkVerdict,
    notes: list[str],
) -> str:
    lines = [
        f"# {_REPORT_LABELS['report_title']}",
        "",
        f"## {_REPORT_LABELS['overview']}",
        *[f"- {line}" for line in overview],
        "",
    ]
    for section in plugin_sections:
        scorecard_metrics = section.scorecard.metric_map()
        process_metric_lines = (
            [
                f"| `{metric}` | `{_format_metric_value(value)}` |"
                for metric, value in sorted(section.process_metrics.items())
            ]
            if section.process_metrics
            else [
                f"| `{_REPORT_LABELS['empty_metric']}` | "
                f"`{_REPORT_LABELS['not_applicable']}` |"
            ]
        )
        sla_lines = (
            [
                (
                    f"- `{rule.severity}`: `{rule.metric_name} {rule.operator} "
                    f"{_format_metric_value(rule.threshold)}` - {rule.reason}"
                )
                for rule in section.sla_rules
            ]
            if section.sla_rules
            else [f"- {_REPORT_LABELS['empty']}"]
        )
        failure_lines = (
            [
                (
                    f"- `{record.metric_name}`: 期望：{record.expected_behavior}；"
                    f"实际：{_normalize_actual_behavior(record.actual_behavior)}"
                )
                for record in section.failure_records
            ]
            if section.failure_records
            else [f"- {_REPORT_LABELS['empty']}"]
        )
        benefit_lines = (
            [f"- `{line}`" for line in section.benefit_direction]
            if section.benefit_direction
            else [f"- {_REPORT_LABELS['no_baseline_comparison']}"]
        )
        lines.extend(
            [
                f"## {section.plugin}",
                "",
                f"### {_REPORT_LABELS['core_scorecard']}",
                f"| {_REPORT_LABELS['metric_header']} | {_REPORT_LABELS['value_header']} |",
                "| --- | --- |",
                *[
                    f"| `{metric}` | `{_format_metric_value(value)}` |"
                    for metric, value in scorecard_metrics.items()
                ],
                "",
                f"### {_REPORT_LABELS['process_metrics']}",
                f"| {_REPORT_LABELS['metric_header']} | {_REPORT_LABELS['value_header']} |",
                "| --- | --- |",
                *process_metric_lines,
                "",
                f"### {_REPORT_LABELS['sla_rules']}",
                *sla_lines,
                "",
                f"### {_REPORT_LABELS['failure_explanations']}",
                *failure_lines,
                "",
                f"### {_REPORT_LABELS['benefit_direction']}",
                *benefit_lines,
                "",
            ]
        )
    warning_lines = (
        [
            (
                f"- {_REPORT_LABELS['warning']} "
                f"`{warning.plugin}.{warning.metric_name}`："
                f"{_normalize_actual_behavior(warning.actual_behavior)}"
            )
            for warning in verdict.warnings
        ]
        if verdict.warnings
        else [f"- {_REPORT_LABELS['no_warnings']}"]
    )
    lines.extend(
        [
            f"## {_REPORT_LABELS['notes']}",
            *[f"- {line}" for line in notes],
            *warning_lines,
        ]
    )
    return "\n".join(lines)


def _format_metric_value(value: float | str) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
