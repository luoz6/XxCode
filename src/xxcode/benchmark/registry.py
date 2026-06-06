from __future__ import annotations

from .models import BenchmarkPlugin


class BenchmarkRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, BenchmarkPlugin] = {}

    def register(self, name: str, plugin: BenchmarkPlugin) -> None:
        self._plugins[name] = plugin

    def get(self, name: str) -> BenchmarkPlugin:
        return self._plugins[name]

    def names(self) -> list[str]:
        return sorted(self._plugins)
