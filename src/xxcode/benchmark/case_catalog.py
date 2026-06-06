from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BenchmarkTier(StrEnum):
    SMOKE = "smoke"
    CORE = "core"
    STRESS = "stress"


class BenchmarkPluginName(StrEnum):
    MEMORY = "memory"
    CONTEXT = "context"
    SECURITY = "security"


class VariantExpectation(StrEnum):
    CANDIDATE_ONLY = "candidate_only"
    BASELINE_VS_CANDIDATE = "baseline_vs_candidate"


@dataclass(frozen=True)
class BenchmarkCaseSpec:
    case_id: str
    tier: BenchmarkTier
    plugin: BenchmarkPluginName
    scenario: str
    variant_expectation: VariantExpectation
    expected_metrics: dict[str, str] = field(default_factory=dict)
    expected_failure_categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    execution_case_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
