from .context import CONTEXT_CASE_SPECS
from .memory import MEMORY_CASE_SPECS
from .security import SECURITY_CASE_SPECS
from xxcode.benchmark import BenchmarkPluginName, BenchmarkTier

ALL_CASE_SPECS = [
    *MEMORY_CASE_SPECS,
    *CONTEXT_CASE_SPECS,
    *SECURITY_CASE_SPECS,
]


def case_specs_for(
    plugin: BenchmarkPluginName,
    tiers: tuple[str, ...],
):
    return [
        case
        for case in ALL_CASE_SPECS
        if case.plugin == plugin and case.tier.value in tiers
    ]


def execution_case_ids_for(
    plugin: BenchmarkPluginName,
    tiers: tuple[str, ...],
    group: str,
) -> set[str]:
    selected: set[str] = set()
    for case in case_specs_for(plugin, tiers):
        selected.update(case.execution_case_ids.get(group, ()))
    return selected

__all__ = [
    "ALL_CASE_SPECS",
    "CONTEXT_CASE_SPECS",
    "MEMORY_CASE_SPECS",
    "SECURITY_CASE_SPECS",
    "case_specs_for",
    "execution_case_ids_for",
]
