from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from xxcode.memory.index import (
    INDEX_FILENAME,
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    parse_memory_index,
)
from xxcode.memory.recall import MAX_RECALLED_MEMORIES


_AVAILABLE_MEMORIES_HEADER = "Available memories:"
_INDEXED_MANIFEST_RE = re.compile(
    r"^- \[indexed\]\s+(?P<filename>[^\s:]+\.md):\s*(?P<description>.*)$"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RecallEvalCase:
    case_id: str
    query: str
    index_content: str
    memory_files: dict[str, str]
    expected_filenames: set[str]
    expected_top1: str | None = None


def validate_case(case: RecallEvalCase) -> None:
    index_bytes = len(case.index_content.encode("utf-8"))
    index_lines = len(case.index_content.rstrip("\n").splitlines())
    if index_lines > MAX_ENTRYPOINT_LINES:
        raise ValueError(
            f"{case.case_id}: index_content exceeds MAX_ENTRYPOINT_LINES"
        )
    if index_bytes > MAX_ENTRYPOINT_BYTES:
        raise ValueError(
            f"{case.case_id}: index_content exceeds MAX_ENTRYPOINT_BYTES"
        )

    indexed_filenames = {
        entry.filename for entry in parse_memory_index(case.index_content)
    }
    memory_filenames = set(case.memory_files)
    missing_memory_files = indexed_filenames - memory_filenames
    if missing_memory_files:
        missing = ", ".join(sorted(missing_memory_files))
        raise ValueError(
            f"{case.case_id}: index_content references files missing from "
            f"memory_files: {missing}"
        )

    missing_expected_files = case.expected_filenames - memory_filenames
    if missing_expected_files:
        missing = ", ".join(sorted(missing_expected_files))
        raise ValueError(
            f"{case.case_id}: expected filenames missing from memory_files: {missing}"
        )

    unindexed_expected_files = case.expected_filenames - indexed_filenames
    if unindexed_expected_files:
        missing = ", ".join(sorted(unindexed_expected_files))
        raise ValueError(
            f"{case.case_id}: expected filenames missing from index_content: {missing}"
        )

    if (
        case.expected_top1 is not None
        and case.expected_top1 not in case.expected_filenames
    ):
        raise ValueError(
            f"{case.case_id}: expected_top1 must be included in expected_filenames"
        )


def materialize_case(case: RecallEvalCase, memory_dir: Path) -> None:
    validate_case(case)
    memory_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in case.memory_files.items():
        (memory_dir / filename).write_text(content, encoding="utf-8")
    (memory_dir / INDEX_FILENAME).write_text(case.index_content, encoding="utf-8")


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
            candidates.append((
                match.group("filename"),
                match.group("description"),
            ))
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
