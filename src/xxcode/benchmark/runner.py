from __future__ import annotations

from collections.abc import Iterable

from .core import BenchmarkCore, BenchmarkReport
from .models import BenchmarkPlugin, BenchmarkScorecard, VariantOverride
from .profiles import get_profile


async def build_benchmark_report(
    plugins: list[BenchmarkPlugin[BenchmarkScorecard]],
    *,
    variant: VariantOverride | None = None,
    baseline_plugins: list[BenchmarkPlugin[BenchmarkScorecard]] | None = None,
    baseline_variant: VariantOverride | None = None,
    baseline_profile: str | None = None,
    tiers: Iterable[str] | None = None,
) -> BenchmarkReport:
    core = BenchmarkCore()
    active_variant = _with_tiers(
        variant or VariantOverride("candidate", "benchmark run"),
        tiers,
    )
    plugin_results: dict[str, object] = {}
    plugin_rules: dict[str, list] = {}
    for plugin in plugins:
        plugin_results[plugin.name] = await core.run_suite(plugin, active_variant)
        plugin_rules[plugin.name] = plugin.build_sla_rules()
    run = core.build_run(active_variant, plugin_results)
    verdict = core.evaluate(run, plugin_rules=plugin_rules)

    comparison = None
    if baseline_plugins is not None:
        active_baseline_variant = _with_tiers(
            (
                get_profile(baseline_profile)
                if baseline_profile is not None
                else baseline_variant or VariantOverride(
                    "baseline",
                    "benchmark baseline",
                )
            ),
            tiers,
        )
        baseline_results: dict[str, object] = {}
        for plugin in baseline_plugins:
            baseline_results[plugin.name] = await core.run_suite(
                plugin,
                active_baseline_variant,
            )
        baseline_run = core.build_run(active_baseline_variant, baseline_results)
        comparison = core.compute_deltas(baseline_run, run)

    return core.build_benchmark_report(
        comparison=comparison,
        verdict=verdict,
        run=run,
        plugin_rules=plugin_rules,
    )


def _with_tiers(
    variant: VariantOverride,
    tiers: Iterable[str] | None,
) -> VariantOverride:
    if tiers is None:
        return variant
    merged = dict(variant.config_overrides or {})
    merged["tiers"] = list(dict.fromkeys(tiers))
    return VariantOverride(
        name=variant.name,
        description=variant.description,
        config_overrides=merged,
        branch_ref=variant.branch_ref,
        artifact_path=variant.artifact_path,
    )
