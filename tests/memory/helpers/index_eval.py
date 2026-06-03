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
    generate_memory_index,
    parse_memory_index,
    truncate_entrypoint_content,
)
from xxcode.memory.models import parse_memory_file


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
class GeneratedIndexEvalCase:
    case_id: str
    memory_files: dict[str, str]
    expected_indexed_filenames: set[str]
    expected_type_order: list[str] | None = None


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


@dataclass(frozen=True)
class IndexOrganizationScorecard:
    n_cases: int
    mean_coverage_rate: float
    mean_stale_reference_rate: float
    mean_duplicate_reference_rate: float
    mean_parseable_line_rate: float
    mean_description_present_rate: float
    mean_description_budget_compliance_rate: float
    mean_generic_description_rate: float
    mean_discriminative_token_rate: float
    mean_line_utilization: float
    mean_byte_utilization: float
    truncated_case_count: int
    type_order_adherence_rate: float


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


def compute_generated_index_metrics(
    case: GeneratedIndexEvalCase,
    memory_dir: Path,
) -> IndexOrganizationMetrics:
    _materialize_generated_case(case, memory_dir)
    index_content = generate_memory_index(memory_dir)
    type_order = _type_order_adherence(index_content, memory_dir, case.expected_type_order)
    metrics = compute_index_metrics(
        index_content,
        case.memory_files,
        case_id=case.case_id,
        type_order_adherence=type_order,
    )
    indexed_filenames = {link.filename for link in scan_raw_index_links(index_content)}
    missing = case.expected_indexed_filenames - indexed_filenames
    if missing:
        raise AssertionError(f"{case.case_id}: missing indexed files: {sorted(missing)}")
    return metrics


def generated_index_cases() -> list[GeneratedIndexEvalCase]:
    return [
        GeneratedIndexEvalCase(
            case_id="generated-type-order",
            memory_files={
                "reference-doc.md": _memory_file(
                    "reference",
                    "Reference Doc",
                    "External docs",
                ),
                "feedback-rule.md": _memory_file(
                    "feedback",
                    "Feedback Rule",
                    "Always run tests",
                ),
                "project-plan.md": _memory_file(
                    "project",
                    "Project Plan",
                    "Project release plan",
                ),
                "user-style.md": _memory_file(
                    "user",
                    "User Style",
                    "User prefers pytest",
                ),
            },
            expected_indexed_filenames={
                "user-style.md",
                "project-plan.md",
                "feedback-rule.md",
                "reference-doc.md",
            },
            expected_type_order=["user", "project", "feedback", "reference"],
        ),
        GeneratedIndexEvalCase(
            case_id="generated-description-signal",
            memory_files={
                "pandas-style.md": _memory_file(
                    "user",
                    "Pandas Style",
                    "User prefers pandas dataframes",
                ),
                "recall-benchmark.md": _memory_file(
                    "project",
                    "Recall Benchmark",
                    "Recall benchmark metrics",
                ),
            },
            expected_indexed_filenames={
                "pandas-style.md",
                "recall-benchmark.md",
            },
            expected_type_order=["user", "project"],
        ),
    ]


def raw_index_risk_cases() -> list[RawIndexEvalCase]:
    return [
        RawIndexEvalCase(
            case_id="raw-stale-reference",
            index_content=(
                "- [Existing](existing.md) - Existing memory\n"
                "- [Ghost](ghost.md) - Missing memory file\n"
            ),
            memory_files={
                "existing.md": _memory_file("user", "Existing", "Existing memory"),
            },
            expected_present_filenames={"existing.md"},
            risk_labels={"stale"},
        ),
        RawIndexEvalCase(
            case_id="raw-duplicate-reference",
            index_content=(
                "- [Existing](existing.md) - Existing memory\n"
                "- [Existing Again](existing.md) - Existing memory duplicate\n"
            ),
            memory_files={
                "existing.md": _memory_file("user", "Existing", "Existing memory"),
            },
            expected_present_filenames={"existing.md"},
            risk_labels={"duplicate"},
        ),
        RawIndexEvalCase(
            case_id="raw-generic-description",
            index_content="- [Todo](todo.md) - todo\n",
            memory_files={
                "todo.md": _memory_file("reference", "Todo", "todo"),
            },
            expected_present_filenames={"todo.md"},
            risk_labels={"generic-description"},
        ),
    ]


def build_index_scorecard(
    metrics: list[IndexOrganizationMetrics],
) -> IndexOrganizationScorecard:
    if not metrics:
        return IndexOrganizationScorecard(
            n_cases=0,
            mean_coverage_rate=0.0,
            mean_stale_reference_rate=0.0,
            mean_duplicate_reference_rate=0.0,
            mean_parseable_line_rate=0.0,
            mean_description_present_rate=0.0,
            mean_description_budget_compliance_rate=0.0,
            mean_generic_description_rate=0.0,
            mean_discriminative_token_rate=0.0,
            mean_line_utilization=0.0,
            mean_byte_utilization=0.0,
            truncated_case_count=0,
            type_order_adherence_rate=0.0,
        )

    n_cases = len(metrics)
    ordered = [
        metric.type_order_adherence
        for metric in metrics
        if metric.type_order_adherence is not None
    ]
    return IndexOrganizationScorecard(
        n_cases=n_cases,
        mean_coverage_rate=sum(m.coverage_rate for m in metrics) / n_cases,
        mean_stale_reference_rate=sum(m.stale_reference_rate for m in metrics)
        / n_cases,
        mean_duplicate_reference_rate=sum(m.duplicate_reference_rate for m in metrics)
        / n_cases,
        mean_parseable_line_rate=sum(m.parseable_line_rate for m in metrics)
        / n_cases,
        mean_description_present_rate=sum(m.description_present_rate for m in metrics)
        / n_cases,
        mean_description_budget_compliance_rate=sum(
            m.description_budget_compliance_rate for m in metrics
        )
        / n_cases,
        mean_generic_description_rate=sum(m.generic_description_rate for m in metrics)
        / n_cases,
        mean_discriminative_token_rate=sum(
            m.discriminative_token_rate for m in metrics
        )
        / n_cases,
        mean_line_utilization=sum(m.line_utilization for m in metrics) / n_cases,
        mean_byte_utilization=sum(m.byte_utilization for m in metrics) / n_cases,
        truncated_case_count=sum(
            1 for m in metrics if m.was_line_truncated or m.was_byte_truncated
        ),
        type_order_adherence_rate=sum(ordered) / len(ordered) if ordered else 0.0,
    )


def _materialize_generated_case(
    case: GeneratedIndexEvalCase,
    memory_dir: Path,
) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in case.memory_files.items():
        path = memory_dir / filename
        path.write_text(content, encoding="utf-8")
        if parse_memory_file(path) is None:
            raise ValueError(f"{case.case_id}: invalid memory file {filename}")


def _type_order_adherence(
    index_content: str,
    memory_dir: Path,
    expected_type_order: list[str] | None,
) -> float | None:
    if expected_type_order is None:
        return None

    observed: list[str] = []
    for entry in parse_memory_index(index_content):
        parsed = parse_memory_file(memory_dir / entry.filename)
        if parsed is not None:
            observed.append(parsed.memory_type.value)
    return 1.0 if observed == expected_type_order else 0.0


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


def _memory_file(memory_type: str, name: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{description}\n"
    )
