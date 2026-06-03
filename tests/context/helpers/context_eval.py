from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xxcode.config import Config
from xxcode.context.builder import build_memory_section, build_system_prompt
from xxcode.context.tokens import token_count_with_estimation
from xxcode.memory.injection import (
    MEMORY_INDEX_SOURCE,
    build_memory_index_message,
    strip_memory_context_messages,
)


@dataclass(frozen=True)
class RecallDiagnostics:
    index_injected: bool
    recalled_count: int
    recall_empty: bool


@dataclass(frozen=True)
class CompressionDiagnostics:
    compression_used: bool
    level_reached: int
    summary_injected: bool


@dataclass(frozen=True)
class ContextEvalCase:
    case_id: str
    scenario: str
    cwd_files: dict[str, str]
    messages: list[dict[str, Any]]
    memory_index_content: str
    memory_files: dict[str, str]
    target_turn_index: int
    expected_compression_level: int
    expected_present: list[str]
    expected_absent: list[str]
    expected_recent_present: list[str]
    expected_stale_absent: list[str]
    expected_order: list[tuple[str, str]]
    required_sections: list[str]
    expected_recall_diagnostics: RecallDiagnostics
    expected_compression_diagnostics: CompressionDiagnostics
    budget_expectation: dict[str, int]


@dataclass(frozen=True)
class ContextSnapshot:
    case_id: str
    system_prompt: str
    prepared_messages: list[dict[str, Any]]
    flattened_text_snapshot: str
    structured_snapshot_view: dict[str, Any] | None
    token_counts: dict[str, int]
    recall_diagnostics: RecallDiagnostics
    compression_diagnostics: CompressionDiagnostics


def render_flattened_snapshot(system_prompt: str, prepared_messages: list[dict[str, Any]]) -> str:
    parts = [f"[SYSTEM PROMPT]\n{system_prompt}"]
    for message in prepared_messages:
        role = message.get("role", "unknown")
        text = _render_message_text(message)
        parts.append(f"[MESSAGE role={role}]\n{text}")
    return "\n\n".join(parts)


async def run_context_case(
    case: ContextEvalCase,
    *,
    memory_dir: Path,
    cwd: Path,
) -> ContextSnapshot:
    _materialize_cwd(case, cwd)
    _materialize_memory(case, memory_dir)
    config = _build_config(cwd, memory_dir)
    system_prompt = build_system_prompt(
        cwd,
        memory_section=build_memory_section(config),
    )
    prepared_messages = list(case.messages[: case.target_turn_index + 1])
    prepared_messages = strip_memory_context_messages(
        prepared_messages,
        source=MEMORY_INDEX_SOURCE,
    )
    index_message = build_memory_index_message(memory_dir)
    index_injected = index_message is not None
    if index_message is not None:
        _insert_before_current_user_message(prepared_messages, index_message)
    flattened = render_flattened_snapshot(system_prompt, prepared_messages)
    token_counts = {
        "prepared_messages_tokens": token_count_with_estimation(prepared_messages),
        "flattened_snapshot_tokens": max(1, len(flattened) // 4),
    }
    return ContextSnapshot(
        case_id=case.case_id,
        system_prompt=system_prompt,
        prepared_messages=prepared_messages,
        flattened_text_snapshot=flattened,
        structured_snapshot_view=None,
        token_counts=token_counts,
        recall_diagnostics=RecallDiagnostics(
            index_injected=index_injected,
            recalled_count=0,
            recall_empty=True,
        ),
        compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
    )


def _materialize_cwd(case: ContextEvalCase, cwd: Path) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    for relative_path, content in case.cwd_files.items():
        target = cwd / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _materialize_memory(case: ContextEvalCase, memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    if case.memory_index_content:
        (memory_dir / "MEMORY.md").write_text(case.memory_index_content, encoding="utf-8")
    for filename, content in case.memory_files.items():
        (memory_dir / filename).write_text(content, encoding="utf-8")


def _build_config(cwd: Path, memory_dir: Path) -> Config:
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=cwd,
        session_dir=cwd / "sessions",
        auto_memory_enabled=True,
        auto_memory_directory=str(memory_dir),
    )


def _render_message_text(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            parts.append(block.get("text", ""))
        elif block_type == "tool_result":
            parts.append(str(block.get("content", "")))
        elif block_type == "tool_use":
            parts.append(f"[tool_use:{block.get('name', '')}]")
    return "\n".join(part for part in parts if part)


def _insert_before_current_user_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    if messages and messages[-1].get("role") == "user":
        messages.insert(len(messages) - 1, message)
    else:
        messages.append(message)
