from __future__ import annotations

import pytest

from xxcode.benchmark import available_profiles, get_profile


def test_available_profiles_lists_fixed_baselines():
    assert available_profiles() == [
        "context_off",
        "memory_off",
        "security_relaxed",
    ]


def test_memory_off_profile_disables_all_memory_mechanisms():
    profile = get_profile("memory_off")

    assert profile.name == "baseline"
    assert profile.config_overrides == {
        "disable_memory_recall": True,
        "disable_memory_extraction": True,
        "disable_memory_index": True,
        "disable_memory_effectiveness": True,
    }


def test_context_off_profile_disables_context_optimizations():
    profile = get_profile("context_off")

    assert profile.config_overrides == {
        "disable_context_optimizations": True,
    }


def test_security_relaxed_profile_disables_security_guards():
    profile = get_profile("security_relaxed")

    assert profile.config_overrides == {
        "disable_static_checks": True,
        "disable_classifier": True,
        "disable_sandbox": True,
        "disable_secret_guard": True,
    }


def test_unknown_profile_raises_clear_error():
    with pytest.raises(ValueError, match="unknown benchmark profile"):
        get_profile("missing-profile")
