from __future__ import annotations

from collections import Counter

from xxcode.benchmark import BenchmarkPluginName, BenchmarkTier, VariantExpectation
from tests.benchmark.catalogs import (
    ALL_CASE_SPECS,
    CONTEXT_CASE_SPECS,
    MEMORY_CASE_SPECS,
    SECURITY_CASE_SPECS,
)


def test_case_catalog_ids_are_unique():
    case_ids = [case.case_id for case in ALL_CASE_SPECS]

    assert len(case_ids) == len(set(case_ids))


def test_case_catalog_has_expected_plugin_counts():
    counts = Counter(case.plugin for case in ALL_CASE_SPECS)

    assert counts == {
        BenchmarkPluginName.MEMORY: 9,
        BenchmarkPluginName.CONTEXT: 9,
        BenchmarkPluginName.SECURITY: 9,
    }


def test_case_catalog_has_expected_tier_counts():
    counts = Counter(case.tier for case in ALL_CASE_SPECS)

    assert counts == {
        BenchmarkTier.SMOKE: 9,
        BenchmarkTier.CORE: 13,
        BenchmarkTier.STRESS: 5,
    }


def test_case_catalogs_only_use_known_variant_expectations():
    assert {case.variant_expectation for case in ALL_CASE_SPECS} == {
        VariantExpectation.CANDIDATE_ONLY,
        VariantExpectation.BASELINE_VS_CANDIDATE,
    }


def test_case_catalogs_all_define_execution_mappings():
    assert all(case.execution_case_ids for case in ALL_CASE_SPECS)


def test_memory_catalog_is_memory_only():
    assert all(case.plugin == BenchmarkPluginName.MEMORY for case in MEMORY_CASE_SPECS)


def test_context_catalog_is_context_only():
    assert all(case.plugin == BenchmarkPluginName.CONTEXT for case in CONTEXT_CASE_SPECS)


def test_security_catalog_is_security_only():
    assert all(case.plugin == BenchmarkPluginName.SECURITY for case in SECURITY_CASE_SPECS)
