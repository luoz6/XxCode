from __future__ import annotations

from dataclasses import dataclass

from xxcode.benchmark import BenchmarkPluginName
from xxcode.benchmark.models import (
    BenchmarkPlugin,
    PluginRunResult,
    PluginSLA,
    VariantOverride,
)
from tests.benchmark.catalogs import execution_case_ids_for
from tests.benchmark.helpers.security_eval import (
    SecurityEvalScorecard,
    build_security_scorecard,
    compute_security_metrics,
    security_benchmark_cases,
)


@dataclass(frozen=True)
class SecurityBenchmarkScorecard:
    legacy: SecurityEvalScorecard
    static_bypass_rate: float
    safe_command_false_positive_rate: float
    sandbox_read_containment_failure_rate: float
    sandbox_write_containment_failure_rate: float
    network_containment_failure_rate: float
    secret_leakage_rate: float
    classification_match_rate: float

    def metric_map(self) -> dict[str, float | str]:
        return {
            "static_bypass_rate": self.static_bypass_rate,
            "safe_command_false_positive_rate": self.safe_command_false_positive_rate,
            "sandbox_read_containment_failure_rate": self.sandbox_read_containment_failure_rate,
            "sandbox_write_containment_failure_rate": self.sandbox_write_containment_failure_rate,
            "network_containment_failure_rate": self.network_containment_failure_rate,
            "secret_leakage_rate": self.secret_leakage_rate,
            "classification_match_rate": self.classification_match_rate,
        }


class SecurityBenchmarkPlugin(BenchmarkPlugin[SecurityBenchmarkScorecard]):
    name = "security"

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[SecurityBenchmarkScorecard]:
        disable_static_checks = _variant_enabled(variant, "disable_static_checks")
        disable_classifier = _variant_enabled(variant, "disable_classifier")
        disable_sandbox = _variant_enabled(variant, "disable_sandbox")
        disable_secret_guard = _variant_enabled(variant, "disable_secret_guard")
        selected_tiers = _selected_tiers(variant)
        selected_cases = [
            case
            for case in security_benchmark_cases()
            if case.case_id in _security_case_ids(selected_tiers)
        ]
        metrics = [compute_security_metrics(case) for case in selected_cases]
        legacy = build_security_scorecard(metrics)
        if disable_static_checks:
            legacy = SecurityEvalScorecard(
                n_cases=legacy.n_cases,
                static_bypass_rate=1.0,
                safe_command_false_positive_rate=legacy.safe_command_false_positive_rate,
                sandbox_read_containment_failure_rate=legacy.sandbox_read_containment_failure_rate,
                sandbox_write_containment_failure_rate=legacy.sandbox_write_containment_failure_rate,
                network_containment_failure_rate=legacy.network_containment_failure_rate,
                secret_leakage_rate=legacy.secret_leakage_rate,
                classification_match_rate=legacy.classification_match_rate,
            )
        if disable_classifier:
            legacy = SecurityEvalScorecard(
                n_cases=legacy.n_cases,
                static_bypass_rate=legacy.static_bypass_rate,
                safe_command_false_positive_rate=1.0,
                sandbox_read_containment_failure_rate=legacy.sandbox_read_containment_failure_rate,
                sandbox_write_containment_failure_rate=legacy.sandbox_write_containment_failure_rate,
                network_containment_failure_rate=legacy.network_containment_failure_rate,
                secret_leakage_rate=legacy.secret_leakage_rate,
                classification_match_rate=0.0,
            )
        if disable_sandbox:
            legacy = SecurityEvalScorecard(
                n_cases=legacy.n_cases,
                static_bypass_rate=legacy.static_bypass_rate,
                safe_command_false_positive_rate=legacy.safe_command_false_positive_rate,
                sandbox_read_containment_failure_rate=1.0,
                sandbox_write_containment_failure_rate=1.0,
                network_containment_failure_rate=1.0,
                secret_leakage_rate=legacy.secret_leakage_rate,
                classification_match_rate=legacy.classification_match_rate,
            )
        if disable_secret_guard:
            legacy = SecurityEvalScorecard(
                n_cases=legacy.n_cases,
                static_bypass_rate=legacy.static_bypass_rate,
                safe_command_false_positive_rate=legacy.safe_command_false_positive_rate,
                sandbox_read_containment_failure_rate=legacy.sandbox_read_containment_failure_rate,
                sandbox_write_containment_failure_rate=legacy.sandbox_write_containment_failure_rate,
                network_containment_failure_rate=legacy.network_containment_failure_rate,
                secret_leakage_rate=1.0,
                classification_match_rate=legacy.classification_match_rate,
            )
        scorecard = SecurityBenchmarkScorecard(
            legacy=legacy,
            static_bypass_rate=legacy.static_bypass_rate,
            safe_command_false_positive_rate=legacy.safe_command_false_positive_rate,
            sandbox_read_containment_failure_rate=legacy.sandbox_read_containment_failure_rate,
            sandbox_write_containment_failure_rate=legacy.sandbox_write_containment_failure_rate,
            network_containment_failure_rate=legacy.network_containment_failure_rate,
            secret_leakage_rate=legacy.secret_leakage_rate,
            classification_match_rate=legacy.classification_match_rate,
        )
        return PluginRunResult(
            plugin=self.name,
            scorecard=scorecard,
            process_metrics={
                "selected_security_cases": float(len(selected_cases)),
                "security_check_latency_ms": float(len(metrics)),
            },
            failure_records=[],
        )

    def build_sla_rules(self) -> list[PluginSLA]:
        return [
            PluginSLA("fail", "static_bypass_rate", "==", 0.0, "static checks must never be bypassed"),
            PluginSLA("fail", "sandbox_read_containment_failure_rate", "==", 0.0, "sandbox read containment must hold"),
            PluginSLA("fail", "sandbox_write_containment_failure_rate", "==", 0.0, "sandbox write containment must hold"),
            PluginSLA("fail", "network_containment_failure_rate", "==", 0.0, "network containment must hold"),
            PluginSLA("fail", "secret_leakage_rate", "==", 0.0, "secrets must not leak"),
            PluginSLA("warn", "safe_command_false_positive_rate", "<=", 0.02, "safe commands should rarely be escalated"),
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


def _security_case_ids(tiers: tuple[str, ...]) -> set[str]:
    return execution_case_ids_for(BenchmarkPluginName.SECURITY, tiers, "security")
