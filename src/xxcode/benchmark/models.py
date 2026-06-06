from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar, runtime_checkable


@runtime_checkable
class BenchmarkScorecard(Protocol):
    def metric_map(self) -> dict[str, float | str]: ...


ScorecardT = TypeVar("ScorecardT", bound=BenchmarkScorecard)


@dataclass(frozen=True)
class FailureRecord:
    case_id: str
    plugin: str
    category: str
    severity: str
    metric_name: str
    expected_behavior: str
    actual_behavior: str
    baseline_behavior: str | None = None
    observed_value: float | str | None = None
    expected_value: float | str | None = None
    reproduction_cmd: str | None = None
    trace_id: str | None = None
    replay_log_path: str | None = None


@dataclass(frozen=True)
class PluginSLA:
    severity: str
    metric_name: str
    operator: str
    threshold: float
    reason: str


@dataclass(frozen=True)
class VariantOverride:
    name: str
    description: str
    config_overrides: dict[str, object] | None = None
    branch_ref: str | None = None
    artifact_path: str | None = None


@dataclass(frozen=True)
class PluginRunResult(Generic[ScorecardT]):
    plugin: str
    scorecard: ScorecardT
    process_metrics: dict[str, float]
    failure_records: list[FailureRecord]


@runtime_checkable
class BenchmarkPlugin(Protocol[ScorecardT]):
    name: str

    async def run_suite(
        self,
        variant: VariantOverride | None,
    ) -> PluginRunResult[ScorecardT]: ...

    def build_sla_rules(self) -> list[PluginSLA]: ...
