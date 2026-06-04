"""XxCode CLI terminal UI entrypoints."""

from __future__ import annotations

from ..config import Config
from .fullscreen_ui import PromptToolkitFullscreenUI
from .terminal_ui import XxCodeTerminalUI


def create_ui(config: Config):
    if getattr(config, "ui_backend", "legacy_terminal") == "prompt_toolkit_fullscreen":
        return PromptToolkitFullscreenUI(config)
    return XxCodeTerminalUI(config)


__all__ = ["XxCodeTerminalUI", "PromptToolkitFullscreenUI", "create_ui"]
