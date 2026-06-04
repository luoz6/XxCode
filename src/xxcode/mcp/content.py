"""MCP content-part extraction helpers."""

from __future__ import annotations

from typing import Any


def extract_content(parts: list[dict[str, Any]]) -> str:
    """Extract readable text from MCP content parts.

    Non-text payloads are currently summarized for model consumption. Keeping
    this logic separate from ``McpClient`` leaves room for richer image/blob
    and embedded-resource handling later.
    """
    if not parts:
        return ""

    lines: list[str] = []
    for item in parts:
        item_type = item.get("type", "text")
        if item_type == "text":
            lines.append(item.get("text", ""))
        elif item_type == "image":
            data = item.get("data", "")
            mime = item.get("mimeType", "image/png")
            lines.append(f"[Image: {mime}, {len(data)} bytes base64]")
        elif item_type == "resource":
            resource = item.get("resource", {})
            uri = resource.get("uri", "?") if isinstance(resource, dict) else str(resource)
            lines.append(f"[Resource: {uri}]")
        else:
            lines.append(f"[Unknown content type: {item_type}]")

    return "\n".join(lines)


__all__ = ["extract_content"]
