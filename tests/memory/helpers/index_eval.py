from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from xxcode.memory.index import (
    INDEX_FILENAME,
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    parse_memory_index,
    truncate_entrypoint_content,
)


_RAW_LINK_RE = re.compile(
    r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)\s*(?P<tail>.*)$"
)
_GENERIC_DESCRIPTIONS = {
    "general",
    "information",
    "memory",
    "misc",
    "note",
    "stuff",
    "todo",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RawIndexLink:
    title: str
    filename: str
    description: str
    line: str


@dataclass(frozen=True)
class RawIndexEvalCase:
    case_id: str
    index_content: str
    memory_files: dict[str, str]
    expected_present_filenames: set[str]
    risk_labels: set[str] | None = None


@dataclass(frozen=True)
class IndexOrganizationMetrics:
    case_id: str
    indexed_file_count: int
    memory_file_count: int
    coverage_rate: float
    stale_reference_rate: float
    duplicate_reference_rate: float
    parseable_line_rate: float
    memory_md_exclusion: float
    description_present_rate: float
    description_budget_compliance_rate: float
    generic_description_rate: float
    discriminative_token_rate: float
    line_utilization: float
    byte_utilization: float
    was_line_truncated: bool
    was_byte_truncated: bool
    type_order_adherence: float | None = None


def scan_raw_index_links(index_content: str) -> list[RawIndexLink]:
    links: list[RawIndexLink] = []
    for line in index_content.splitlines():
        match = _RAW_LINK_RE.match(line)
        if not match:
            continue
        tail = match.group("tail").strip()
        description = re.sub(r"^(?:[-:]\s*)+", "", tail).strip()
        links.append(
            RawIndexLink(
                title=match.group("title").strip(),
                filename=Path(match.group("file")).name,
                description=description,
                line=line,
            )
        )
    return links


def compute_index_metrics(
    index_content: str,
    memory_files: dict[str, str],
    *,
    case_id: str = "index-case",
    type_order_adherence: float | None = None,
) -> IndexOrganizationMetrics:
    raw_links = scan_raw_index_links(index_content)
    runtime_entries = parse_memory_index(index_content)
    raw_filenames = [link.filename for link in raw_links]
    raw_reference_count = len(raw_filenames)
    memory_filenames = {
        filename for filename in memory_files if filename != INDEX_FILENAME
    }
    parsed_filenames = {
        entry.filename for entry in runtime_entries if entry.filename != INDEX_FILENAME
    }
    existing_indexed = parsed_filenames & memory_filenames
    stale_references = [
        filename
        for filename in raw_filenames
        if filename != INDEX_FILENAME and filename not in memory_filenames
    ]
    candidate_lines = [
        line for line in index_content.splitlines() if line.lstrip().startswith("-")
    ]
    truncation = truncate_entrypoint_content(index_content)

    return IndexOrganizationMetrics(
        case_id=case_id,
        indexed_file_count=len(parsed_filenames),
        memory_file_count=len(memory_filenames),
        coverage_rate=_safe_div(len(existing_indexed), len(memory_filenames), 1.0),
        stale_reference_rate=_safe_div(
            len(stale_references),
            raw_reference_count,
            0.0,
        ),
        duplicate_reference_rate=_safe_div(
            _duplicate_count(raw_filenames),
            raw_reference_count,
            0.0,
        ),
        parseable_line_rate=_safe_div(
            len(raw_links),
            len(candidate_lines),
            1.0,
        ),
        memory_md_exclusion=0.0 if INDEX_FILENAME in raw_filenames else 1.0,
        description_present_rate=_description_present_rate(runtime_entries),
        description_budget_compliance_rate=_description_budget_compliance_rate(
            raw_links
        ),
        generic_description_rate=_generic_description_rate(runtime_entries),
        discriminative_token_rate=_discriminative_token_rate(runtime_entries),
        line_utilization=truncation.line_count / MAX_ENTRYPOINT_LINES,
        byte_utilization=truncation.byte_count / MAX_ENTRYPOINT_BYTES,
        was_line_truncated=truncation.was_line_truncated,
        was_byte_truncated=truncation.was_byte_truncated,
        type_order_adherence=type_order_adherence,
    )


def _duplicate_count(values: list[str]) -> int:
    return sum(count - 1 for count in Counter(values).values() if count > 1)


def _safe_div(numerator: int, denominator: int, empty_value: float) -> float:
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _description_present_rate(entries: Iterable) -> float:
    entries = list(entries)
    return _safe_div(
        sum(1 for entry in entries if entry.description.strip()),
        len(entries),
        1.0,
    )


def _description_budget_compliance_rate(raw_links: list[RawIndexLink]) -> float:
    from xxcode.memory import index as index_module

    line_budget = getattr(index_module, "_LINE_BUDGET", 150)
    return _safe_div(
        sum(1 for link in raw_links if len(link.line) <= line_budget),
        len(raw_links),
        1.0,
    )


def _generic_description_rate(entries: Iterable) -> float:
    entries = list(entries)
    generic = 0
    for entry in entries:
        normalized = entry.description.strip().lower()
        if normalized in _GENERIC_DESCRIPTIONS:
            generic += 1
    return _safe_div(generic, len(entries), 0.0)


def _discriminative_token_rate(entries: Iterable) -> float:
    entries = list(entries)
    discriminative = 0
    for entry in entries:
        filename_tokens = _tokens(Path(entry.filename).stem.replace("-", " "))
        description_tokens = _tokens(entry.description)
        if description_tokens - filename_tokens:
            discriminative += 1
    return _safe_div(discriminative, len(entries), 1.0)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))
