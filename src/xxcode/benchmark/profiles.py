from __future__ import annotations

from .models import VariantOverride


_PROFILES: dict[str, VariantOverride] = {
    "memory_off": VariantOverride(
        "baseline",
        "memory mechanisms disabled",
        config_overrides={
            "disable_memory_recall": True,
            "disable_memory_extraction": True,
            "disable_memory_index": True,
            "disable_memory_effectiveness": True,
        },
    ),
    "context_off": VariantOverride(
        "baseline",
        "context optimizations disabled",
        config_overrides={
            "disable_context_optimizations": True,
        },
    ),
    "security_relaxed": VariantOverride(
        "baseline",
        "security protections relaxed",
        config_overrides={
            "disable_static_checks": True,
            "disable_classifier": True,
            "disable_sandbox": True,
            "disable_secret_guard": True,
        },
    ),
}


def available_profiles() -> list[str]:
    return sorted(_PROFILES)


def get_profile(name: str) -> VariantOverride:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown benchmark profile: {name}. "
            f"available={', '.join(available_profiles())}"
        ) from exc
