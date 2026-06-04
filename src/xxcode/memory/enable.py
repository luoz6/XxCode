"""Five-level priority check for whether auto-memory is enabled."""

import os


def _parse_bool_env(name: str) -> bool | None:
    """Parse a boolean environment variable. Returns None if unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    low = value.lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    return None


def is_auto_memory_enabled(
    *,
    config_auto_memory_enabled: bool = True,
    bare_mode: bool = False,
    remote_mode: bool = False,
) -> bool:
    """Check whether auto-memory should be enabled (five-level priority).

    1. ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` env var  → disabled
    2. ``bare_mode`` flag                             → disabled
    3. ``remote_mode`` flag                            → disabled
    4. ``config_auto_memory_enabled``                  → as configured
    5. Default                                         → enabled
    """
    # Level 1: environment variable override
    env_disable = _parse_bool_env("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    if env_disable is True:
        return False

    # Level 2: --bare mode
    if bare_mode:
        return False

    # Level 3: remote mode (no persistent storage)
    if remote_mode:
        return False

    # Level 4: config setting
    if not config_auto_memory_enabled:
        return False

    # Level 5: default
    return True
