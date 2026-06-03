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
from xxcode.memory.recall import MAX_RECALLED_MEMORIES, recall_memories_for_query


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


@dataclass(frozen=True)
class QualityMetrics:
    case_id: str
    selected_filenames: list[str]
    expected_filenames: set[str]
    precision_at_k: float
    recall_at_k: float
    f1_at_k: float
    top1_hit: float
    topk_full_match: float


@dataclass(frozen=True)
class QualityScorecard:
    n_cases: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    mean_f1_at_k: float
    top1_hit_rate: float
    full_match_rate: float


async def run_recall_case(case: RecallEvalCase, memory_dir: Path) -> list[str]:
    materialize_case(case, memory_dir)

    async def _client_factory():
        return DeterministicRecallClient()

    recalled = await recall_memories_for_query(
        query=case.query,
        memory_dir=memory_dir,
        client_factory=_client_factory,
    )
    return [memory.filename for memory in recalled]


def compute_quality_metrics(
    case: RecallEvalCase,
    selected_filenames: list[str],
) -> QualityMetrics:
    expected = set(case.expected_filenames)
    selected_set = set(selected_filenames)
    matched = len(selected_set & expected)

    precision = matched / len(selected_filenames) if selected_filenames else 0.0
    recall = matched / len(expected) if expected else 1.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    if case.expected_top1 is None:
        top1_hit = 1.0
    else:
        top1_hit = (
            1.0
            if selected_filenames and selected_filenames[0] == case.expected_top1
            else 0.0
        )

    return QualityMetrics(
        case_id=case.case_id,
        selected_filenames=selected_filenames,
        expected_filenames=expected,
        precision_at_k=precision,
        recall_at_k=recall,
        f1_at_k=f1,
        top1_hit=top1_hit,
        topk_full_match=1.0 if selected_set == expected else 0.0,
    )


def build_quality_scorecard(metrics: list[QualityMetrics]) -> QualityScorecard:
    if not metrics:
        return QualityScorecard(
            n_cases=0,
            mean_precision_at_k=0.0,
            mean_recall_at_k=0.0,
            mean_f1_at_k=0.0,
            top1_hit_rate=0.0,
            full_match_rate=0.0,
        )

    n_cases = len(metrics)
    return QualityScorecard(
        n_cases=n_cases,
        mean_precision_at_k=sum(m.precision_at_k for m in metrics) / n_cases,
        mean_recall_at_k=sum(m.recall_at_k for m in metrics) / n_cases,
        mean_f1_at_k=sum(m.f1_at_k for m in metrics) / n_cases,
        top1_hit_rate=sum(m.top1_hit for m in metrics) / n_cases,
        full_match_rate=sum(m.topk_full_match for m in metrics) / n_cases,
    )


def quality_benchmark_cases() -> list[RecallEvalCase]:
    return [
        RecallEvalCase(
            case_id="single-obvious-target",
            query="use pandas dataframe analysis",
            index_content=(
                "- [Pandas Style](pandas-style.md) - User prefers pandas "
                "dataframes for analysis\n"
                "- [Release Plan](release-plan.md) - Release deadline planning\n"
            ),
            memory_files={
                "pandas-style.md": _memory_file("user", "Pandas preference"),
                "release-plan.md": _memory_file("project", "Release plan"),
            },
            expected_filenames={"pandas-style.md"},
            expected_top1="pandas-style.md",
        ),
        RecallEvalCase(
            case_id="two-related-memories",
            query="prepare pytest memory recall quality regression tests",
            index_content=(
                "- [Recall Benchmark](recall-benchmark.md) - Memory recall "
                "quality benchmark uses pytest regression metrics\n"
                "- [Testing Style](testing-style.md) - Project prefers pytest "
                "red green refactor tests\n"
                "- [Deployment Note](deployment-note.md) - Production release "
                "checklist\n"
            ),
            memory_files={
                "recall-benchmark.md": _memory_file("project", "Recall benchmark"),
                "testing-style.md": _memory_file("feedback", "Testing style"),
                "deployment-note.md": _memory_file("reference", "Deployment"),
            },
            expected_filenames={"recall-benchmark.md", "testing-style.md"},
            expected_top1="recall-benchmark.md",
        ),
        RecallEvalCase(
            case_id="distractor-resistance",
            query="remember database backups warning",
            index_content=(
                "- [Migration Warning](migration-warning.md) - Database "
                "migration warning requires backups\n"
                "- [Migration Checklist](migration-checklist.md) - UI migration "
                "checklist for layout files\n"
                "- [Shell Reference](shell-reference.md) - Shell command usage "
                "reference\n"
            ),
            memory_files={
                "migration-warning.md": _memory_file("project", "Database warning"),
                "migration-checklist.md": _memory_file("reference", "UI checklist"),
                "shell-reference.md": _memory_file("reference", "Shell reference"),
            },
            expected_filenames={"migration-warning.md"},
            expected_top1="migration-warning.md",
        ),
        RecallEvalCase(
            case_id="description-beats-misleading-filename",
            query="handle backoff policy failures",
            index_content=(
                "- [Api Retry](api-retry.md) - Deprecated API naming note\n"
                "- [Retry Policy](retry-policy.md) - Retry backoff policy uses "
                "exponential backoff for API failures\n"
            ),
            memory_files={
                "api-retry.md": _memory_file("reference", "Deprecated API naming"),
                "retry-policy.md": _memory_file("project", "Retry backoff policy"),
            },
            expected_filenames={"retry-policy.md"},
            expected_top1="retry-policy.md",
        ),
        RecallEvalCase(
            case_id="generic-filename-relevant-description",
            query="recall metrics scorecard details",
            index_content=(
                "- [Note One](note-1.md) - Recall metrics scorecard includes "
                "precision recall f1 and top1 details\n"
                "- [Named Archive](named-archive.md) - Historical packaging "
                "archive\n"
            ),
            memory_files={
                "note-1.md": _memory_file("project", "Recall metric details"),
                "named-archive.md": _memory_file("reference", "Archive"),
            },
            expected_filenames={"note-1.md"},
            expected_top1="note-1.md",
        ),
        RecallEvalCase(
            case_id="cap-pressure",
            query="memory recall quality stability precision recall f1 top1 scorecard",
            index_content=(
                "- [Quality](quality.md) - Memory recall quality precision recall "
                "f1 metric\n"
                "- [Stability](stability.md) - Memory recall stability repeat "
                "order noise robustness\n"
                "- [Top One](top-one.md) - Top1 recall scorecard metric\n"
                "- [Full Match](full-match.md) - Full match recall scorecard "
                "metric\n"
                "- [Case Count](case-count.md) - Scorecard n cases reporting\n"
                "- [Weak Candidate](weak-candidate.md) - Memory note archive\n"
                "- [Other Candidate](other-candidate.md) - General project note\n"
            ),
            memory_files={
                "quality.md": _memory_file("project", "Quality metric"),
                "stability.md": _memory_file("project", "Stability metric"),
                "top-one.md": _memory_file("project", "Top1 metric"),
                "full-match.md": _memory_file("project", "Full match"),
                "case-count.md": _memory_file("project", "Case count"),
                "weak-candidate.md": _memory_file("reference", "Weak candidate"),
                "other-candidate.md": _memory_file("reference", "Other candidate"),
            },
            expected_filenames={
                "quality.md",
                "stability.md",
                "top-one.md",
                "full-match.md",
                "case-count.md",
            },
            expected_top1="quality.md",
        ),
    ]


def _memory_file(memory_type: str, body: str) -> str:
    return (
        "---\n"
        "metadata:\n"
        f"  type: {memory_type}\n"
        "---\n\n"
        f"{body}\n"
    )
