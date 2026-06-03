from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xxcode.config import Config
from xxcode.context.builder import build_memory_section, build_system_prompt
from xxcode.context.pipeline import ContextPipeline
from xxcode.context.tokens import token_count_with_estimation
from xxcode.memory.injection import (
    MEMORY_INDEX_SOURCE,
    build_memory_index_message,
    build_recalled_memories_message,
    strip_memory_context_messages,
)
from xxcode.memory.recall import MAX_RECALLED_MEMORIES, recall_memories_for_query


_AVAILABLE_MEMORIES_HEADER = "Available memories:"
_INDEXED_MANIFEST_RE = re.compile(
    r"^- \[indexed\]\s+(?P<filename>[^\s:]+\.md):\s*(?P<description>.*)$"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


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


class DeterministicRecallClient:
    async def complete(
        self,
        system_prompt: str = "",
        messages: list[dict] | None = None,
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        del system_prompt, max_tokens, tools
        user_message = _first_user_text(messages or [])
        query = _extract_query(user_message)
        candidates = _extract_candidates(user_message)
        selected = _rank_candidates(query, candidates)
        return json.dumps(selected)


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

    recalled = await _run_deterministic_recall(
        query=_current_user_query(prepared_messages),
        memory_dir=memory_dir,
    )
    recalled_message = build_recalled_memories_message(recalled)
    if recalled_message is not None:
        _insert_before_current_user_message(prepared_messages, recalled_message)

    pipeline = ContextPipeline(config)
    prepared_messages, stats = await pipeline.compress(
        prepared_messages,
        current_tokens=None,
        system_prompt=system_prompt,
        context_limit=_derived_context_limit(case),
        threshold=_derived_threshold(case),
    )

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
            recalled_count=len(recalled),
            recall_empty=(len(recalled) == 0),
        ),
        compression_diagnostics=CompressionDiagnostics(
            compression_used=stats.level_reached >= 1,
            level_reached=stats.level_reached,
            summary_injected=("[Conversation summary]" in flattened),
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


def _first_user_text(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                return content
    return ""


def _extract_query(user_message: str) -> str:
    for line in user_message.splitlines():
        if line.startswith("Query:"):
            return line.removeprefix("Query:").strip()
    return ""


def _extract_candidates(user_message: str) -> list[tuple[str, str]]:
    lines = user_message.splitlines()
    try:
        start = lines.index(_AVAILABLE_MEMORIES_HEADER) + 1
    except ValueError as exc:
        raise ValueError("Available memories section not found") from exc

    candidates: list[tuple[str, str]] = []
    for line in lines[start:]:
        if not line.strip():
            break
        match = _INDEXED_MANIFEST_RE.match(line.strip())
        if match:
            candidates.append((match.group("filename"), match.group("description")))
    return candidates


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _rank_candidates(query: str, candidates: list[tuple[str, str]]) -> list[str]:
    query_tokens = _tokens(query)
    ranked: list[tuple[int, str]] = []
    for filename, description in candidates:
        filename_tokens = _tokens(Path(filename).stem.replace("-", " "))
        description_tokens = _tokens(description)
        score = len(query_tokens & filename_tokens)
        score += 2 * len(query_tokens & description_tokens)
        if score > 0:
            ranked.append((-score, filename))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [filename for _score, filename in ranked[:MAX_RECALLED_MEMORIES]]


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


async def _run_deterministic_recall(query: str, memory_dir: Path):
    async def _client_factory():
        return DeterministicRecallClient()

    if not query:
        return []

    return await recall_memories_for_query(
        query=query,
        memory_dir=memory_dir,
        client_factory=_client_factory,
    )


def _current_user_query(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    return block["text"]
    return ""


def _insert_before_current_user_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    if messages and messages[-1].get("role") == "user":
        messages.insert(len(messages) - 1, message)
    else:
        messages.append(message)


def _derived_context_limit(case: ContextEvalCase) -> int:
    soft_limit = case.budget_expectation["soft_limit_tokens"]
    threshold = _derived_threshold(case)
    return int(soft_limit / threshold)


def _derived_threshold(case: ContextEvalCase) -> float:
    # Phase-one convention: non-compression cases use 1.0 so the derived
    # soft limit equals the derived context limit and compression stays off
    # unless a case explicitly sizes itself to require it.
    if case.expected_compression_level >= 1:
        return 0.5
    return 1.0
