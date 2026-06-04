"""Sandbox mode for shell command execution.

macOS: sandbox-exec with profile files.
Linux: bubblewrap (bwrap) with landlock-style restrictions.

The sandbox limits:
  - File system access (read/write only to allowed paths)
  - Network access (can be disabled)
  - Process creation
  - System calls

Commands can opt out via dangerouslyDisableSandbox=True,
subject to policy.
"""

from __future__ import annotations

import logging
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._tokenizer import extract_base_command

logger = logging.getLogger(__name__)


# ── Sandbox configuration ─────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """Configuration for sandboxed execution."""
    enabled: bool = True
    allow_network: bool = False
    allow_write: list[str] = None  # type: ignore
    allow_read: list[str] = None   # type: ignore
    allow_processes: bool = False

    def __post_init__(self):
        if self.allow_write is None:
            self.allow_write = []
        if self.allow_read is None:
            self.allow_read = []


class SandboxManager:
    """Manages sandbox availability and configuration."""

    def __init__(self):
        self._enabled = True
        self._platform = platform.system()
        self._sandbox_available = self._detect_sandbox()

    def _detect_sandbox(self) -> bool:
        """Check if sandbox tools are available on this platform."""
        if self._platform == "Darwin":
            return shutil.which("sandbox-exec") is not None
        elif self._platform == "Linux":
            return shutil.which("bwrap") is not None
        return False

    def is_sandboxing_enabled(self) -> bool:
        return self._enabled and self._sandbox_available

    def disable(self) -> None:
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def get_sandbox_command(
        self, command: str, config: SandboxConfig, cwd: str,
    ) -> list[str]:
        """Build the sandbox wrapper command.

        Returns a list of argv tokens that wrap the original command
        in the platform-specific sandbox.

        Args:
            command: The shell command to execute.
            config: Sandbox configuration.
            cwd: Working directory.

        Returns:
            Shell command list to pass to subprocess.
        """
        if not self.is_sandboxing_enabled():
            return ["sh", "-c", command]

        if self._platform == "Darwin":
            return self._macos_sandbox(command, config, cwd)
        elif self._platform == "Linux":
            return self._linux_sandbox(command, config, cwd)
        else:
            return ["sh", "-c", command]

    def _macos_sandbox(
        self, command: str, config: SandboxConfig, cwd: str,
    ) -> list[str]:
        """Build macOS sandbox-exec command.

        Uses a sandbox profile that restricts file access, network,
        and process creation.
        """
        profile = self._build_macos_profile(config, cwd)
        return ["sandbox-exec", "-p", profile, "sh", "-c", command]

    def _build_macos_profile(self, config: SandboxConfig, cwd: str) -> str:
        """Build a macOS sandbox-exec profile string."""
        lines = ["(version 1)"]

        # Default: deny everything.
        lines.append("(deny default)")

        # Allow process execution (needed for sh -c).
        lines.append("(allow process-exec)")

        # Allow reading system files (dyld, shared libs, etc.).
        lines.append('(allow file-read* (subpath "/usr/lib")')
        lines.append('             (subpath "/usr/share")')
        lines.append('             (subpath "/System/Library")')
        lines.append('             (subpath "/Library/Frameworks")')

        # Allow reading common dev tool paths.
        for path in ["/usr/local", "/opt/homebrew", "/usr/bin", "/bin"]:
            if Path(path).exists():
                lines.append(f'             (subpath "{path}")')

        # Allow the working directory and its children (read+write for workspace).
        lines.append(f'             (subpath "{cwd}")')

        # Temp dirs.
        for tmp in ["/tmp", "/private/tmp"]:
            if Path(tmp).exists():
                lines.append(f'             (subpath "{tmp}")')

        # Additional read paths.
        for path in config.allow_read:
            lines.append(f'             (subpath "{path}")')

        lines.append(")")

        # Write access.
        if config.allow_write:
            lines.append("(allow file-write*")
            for path in config.allow_write:
                lines.append(f'             (subpath "{path}")')
            # Always allow writing to cwd and tmp.
            lines.append(f'             (subpath "{cwd}")')
            for tmp in ["/tmp", "/private/tmp"]:
                lines.append(f'             (subpath "{tmp}")')
            lines.append(")")

        # Network.
        if config.allow_network:
            lines.append("(allow network*)")

        # Process forking.
        if config.allow_processes:
            lines.append("(allow process-fork)")

        # Signal handling.
        lines.append("(allow signal)")

        return "\n".join(lines)

    def _linux_sandbox(
        self, command: str, config: SandboxConfig, cwd: str,
    ) -> list[str]:
        """Build Linux bubblewrap command.

        Uses bwrap to create a restricted namespace with:
          - Private /tmp
          - Read-only /usr, /lib, /etc
          - Network isolation (optional)
          - No new privileges
          - Limited filesystem view
        """
        args = [
            "bwrap",
            "--unshare-all",
            "--clearenv",
            "--new-session",
            "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/etc", "/etc",
            "--ro-bind", "/opt", "/opt",
            "--bind", cwd, cwd,
            "--chdir", cwd,
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
        ]

        # Optional paths.
        if Path("/usr/local").exists():
            args.extend(["--ro-bind", "/usr/local", "/usr/local"])
        if Path("/opt/homebrew").exists():
            args.extend(["--ro-bind", "/opt/homebrew", "/opt/homebrew"])

        # Additional mounts.
        for path in config.allow_read:
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])

        for path in config.allow_write:
            if Path(path).exists():
                args.extend(["--bind", path, path])

        # Network isolation.
        if not config.allow_network:
            args.append("--unshare-net")

        # No new privileges.
        args.extend(["--", "sh", "-c", command])

        return args


# ── Safe command exclusion ────────────────────────────────────────────

# Commands that should never be sandboxed because they need
# full system access to function.
_EXCLUDED_COMMANDS: set[str] = {
    "docker", "podman", "kubectl", "systemctl", "launchctl",
    "brew", "apt", "apt-get", "yum", "dnf", "pacman",
    "snap", "flatpak", "nix", "guix",
    "ssh", "scp", "sftp", "rsync",
    "git",  # Needs ~/.gitconfig, ~/.ssh, etc.
}


def should_use_sandbox(
    command: str,
    manager: SandboxManager,
    dangerously_disable_sandbox: bool = False,
    excluded_commands: set[str] | None = None,
) -> bool:
    """Determine whether a command should run in the sandbox.

    Returns False when:
      - Sandboxing is globally disabled
      - Command is in the excluded list
      - dangerouslyDisableSandbox=True and the platform sandbox is optional
      - Sandbox tool is not available on this platform
    """
    if not manager.is_sandboxing_enabled():
        return False

    if dangerously_disable_sandbox:
        logger.debug("Sandbox bypassed via dangerouslyDisableSandbox")
        return False

    # Check excluded commands.
    exclusions = excluded_commands or _EXCLUDED_COMMANDS
    base = extract_base_command(command)
    if base and base in exclusions:
        logger.debug("Sandbox bypassed for excluded command: %s", base)
        return False

    return True


# _extract_base_command removed — use extract_base_command from _tokenizer instead.
