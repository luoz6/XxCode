"""Deferred tool index for lazy tool discovery and activation."""

from __future__ import annotations

from .base import Tool


class DeferredToolIndex:
    """Owns tools hidden from the default model-visible registry."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get_all(self) -> dict[str, Tool]:
        return dict(self._tools)

    def activate(self, name: str) -> Tool | None:
        return self._tools.pop(name, None)

    def search(self, query: str) -> list[Tool]:
        """Search deferred tools by name and search_hint."""
        if not query or not self._tools:
            return []

        if query.startswith("select:"):
            names = [n.strip() for n in query[7:].split(",") if n.strip()]
            return [self._tools[name] for name in names if name in self._tools]

        prefix = ""
        keywords: list[str] = []
        if query.startswith("+"):
            parts = query[1:].split(None, 1)
            prefix = parts[0].lower()
            if len(parts) > 1:
                keywords = parts[1].lower().split()
        else:
            keywords = query.lower().split()

        scored: list[tuple[int, Tool]] = []
        for name, tool in self._tools.items():
            if prefix and not name.lower().startswith(prefix):
                continue
            score = self._score_tool(tool, keywords)
            if score > 0 or not keywords:
                scored.append((score, tool))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [tool for _score, tool in scored]

    @staticmethod
    def _score_tool(tool: Tool, keywords: list[str]) -> int:
        """Score a tool against keywords. Name matches count 3x."""
        if not keywords:
            return 1
        name_lower = tool.name.lower()
        hint_lower = getattr(tool, "_search_hint", "").lower()
        score = 0
        for keyword in keywords:
            if keyword in name_lower:
                score += 3
            if keyword in hint_lower:
                score += 1
        return score


__all__ = ["DeferredToolIndex"]
