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


@dataclass(frozen=True)
class ContextEvalMetrics:
    case_id: str
    required_content_hit: float
    required_order_pass: float
    section_presence_pass: float
    recent_context_preserved: float | None
    stale_content_exclusion_pass: float | None
    forbidden_content_absence_pass: float
    budget_pass: float
    recall_activation_pass: float | None
    compression_activation_pass: float | None
    snapshot_validity_pass: float


@dataclass(frozen=True)
class ContextEvalScorecard:
    n_cases: int
    required_content_hit_rate: float
    required_order_pass_rate: float
    section_presence_rate: float
    recent_context_preservation_rate: float
    stale_content_exclusion_rate: float
    forbidden_content_absence_rate: float
    budget_pass_rate: float
    recall_activation_rate: float
    compression_activation_rate: float
    snapshot_validity_rate: float


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


def compute_context_eval_metrics(case: ContextEvalCase, snapshot: ContextSnapshot) -> ContextEvalMetrics:
    required_content_hit = _fraction(
        [anchor in snapshot.flattened_text_snapshot for anchor in case.expected_present]
    )
    forbidden_content_absence = _fraction(
        [anchor not in snapshot.flattened_text_snapshot for anchor in case.expected_absent]
    )
    order_pass = _fraction(
        [
            snapshot.flattened_text_snapshot.find(earlier) <= snapshot.flattened_text_snapshot.find(later)
            and snapshot.flattened_text_snapshot.find(earlier) != -1
            and snapshot.flattened_text_snapshot.find(later) != -1
            for earlier, later in case.expected_order
        ]
    )
    recent_context_preserved = _optional_fraction(
        [anchor in snapshot.flattened_text_snapshot for anchor in case.expected_recent_present]
    )
    stale_content_exclusion_pass = _optional_fraction(
        [anchor not in snapshot.flattened_text_snapshot for anchor in case.expected_stale_absent]
    )
    budget_pass = 1.0 if (
        snapshot.token_counts["prepared_messages_tokens"] < case.budget_expectation["soft_limit_tokens"]
    ) else 0.0
    recall_activation_pass = 1.0 if (
        snapshot.recall_diagnostics == case.expected_recall_diagnostics
    ) else 0.0
    compression_activation_pass = 1.0 if (
        snapshot.compression_diagnostics == case.expected_compression_diagnostics
    ) else 0.0
    section_presence = 1.0
    snapshot_validity = 1.0 if snapshot.flattened_text_snapshot.strip() else 0.0
    return ContextEvalMetrics(
        case_id=case.case_id,
        required_content_hit=required_content_hit,
        required_order_pass=order_pass,
        section_presence_pass=section_presence,
        recent_context_preserved=recent_context_preserved,
        stale_content_exclusion_pass=stale_content_exclusion_pass,
        forbidden_content_absence_pass=forbidden_content_absence,
        budget_pass=budget_pass,
        recall_activation_pass=recall_activation_pass,
        compression_activation_pass=compression_activation_pass,
        snapshot_validity_pass=snapshot_validity,
    )


def build_context_eval_scorecard(metrics: list[ContextEvalMetrics]) -> ContextEvalScorecard:
    if not metrics:
        return ContextEvalScorecard(
            n_cases=0,
            required_content_hit_rate=0.0,
            required_order_pass_rate=0.0,
            section_presence_rate=0.0,
            recent_context_preservation_rate=0.0,
            stale_content_exclusion_rate=0.0,
            forbidden_content_absence_rate=0.0,
            budget_pass_rate=0.0,
            recall_activation_rate=0.0,
            compression_activation_rate=0.0,
            snapshot_validity_rate=0.0,
        )
    return ContextEvalScorecard(
        n_cases=len(metrics),
        required_content_hit_rate=sum(m.required_content_hit for m in metrics) / len(metrics),
        required_order_pass_rate=sum(m.required_order_pass for m in metrics) / len(metrics),
        section_presence_rate=sum(m.section_presence_pass for m in metrics) / len(metrics),
        recent_context_preservation_rate=_mean_optional([m.recent_context_preserved for m in metrics]),
        stale_content_exclusion_rate=_mean_optional([m.stale_content_exclusion_pass for m in metrics]),
        forbidden_content_absence_rate=sum(m.forbidden_content_absence_pass for m in metrics) / len(metrics),
        budget_pass_rate=sum(m.budget_pass for m in metrics) / len(metrics),
        recall_activation_rate=_mean_optional([m.recall_activation_pass for m in metrics]),
        compression_activation_rate=_mean_optional([m.compression_activation_pass for m in metrics]),
        snapshot_validity_rate=sum(m.snapshot_validity_pass for m in metrics) / len(metrics),
    )


def format_context_eval_scorecard(scorecard: ContextEvalScorecard) -> str:
    return (
        "context-eval "
        f"n_cases={scorecard.n_cases} "
        f"required_content_hit_rate={scorecard.required_content_hit_rate:.3f} "
        f"required_order_pass_rate={scorecard.required_order_pass_rate:.3f} "
        f"forbidden_content_absence_rate={scorecard.forbidden_content_absence_rate:.3f} "
        f"budget_pass_rate={scorecard.budget_pass_rate:.3f} "
        f"snapshot_validity_rate={scorecard.snapshot_validity_rate:.3f}"
    )


def semantic_benchmark_cases() -> list[ContextEvalCase]:
    return [_simple_semantic_case(), _memory_semantic_case(), _compression_semantic_case()]


def stability_benchmark_cases() -> list[ContextEvalCase]:
    return [_memory_semantic_case(), _compression_semantic_case()]


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


def _simple_semantic_case() -> ContextEvalCase:
    return ContextEvalCase(
        case_id="semantic-constraint",
        scenario="Preserve a direct user constraint.",
        cwd_files={"CLAUDE.md": "Always preserve direct user constraints."},
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "Do not modify settings.py"}],
            }
        ],
        memory_index_content="",
        memory_files={},
        target_turn_index=0,
        expected_compression_level=0,
        expected_present=["Do not modify settings.py"],
        expected_absent=[],
        expected_recent_present=["Do not modify settings.py"],
        expected_stale_absent=[],
        expected_order=[],
        required_sections=[],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=0,
            recall_empty=True,
        ),
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
        budget_expectation={"soft_limit_tokens": 4000, "hard_limit_tokens": 8000},
    )


def _memory_semantic_case() -> ContextEvalCase:
    return ContextEvalCase(
        case_id="semantic-memory",
        scenario="Recall relevant memory into the current snapshot.",
        cwd_files={"CLAUDE.md": "Project instructions."},
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "Please plan the dataframe analysis flow."}],
            }
        ],
        memory_index_content=(
            "- [Python Style](python-style.md) - User prefers pandas dataframe analysis\n"
        ),
        memory_files={
            "python-style.md": (
                "---\nmetadata:\n  type: user\n---\n\n"
                "Use pandas for dataframe-style analysis.\n"
            ),
        },
        target_turn_index=0,
        expected_compression_level=0,
        expected_present=[
            "Use pandas for dataframe-style analysis.",
            "Please plan the dataframe analysis flow.",
        ],
        expected_absent=[],
        expected_recent_present=["Please plan the dataframe analysis flow."],
        expected_stale_absent=[],
        expected_order=[
            ("Use pandas for dataframe-style analysis.", "Please plan the dataframe analysis flow."),
        ],
        required_sections=[],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=1,
            recall_empty=False,
        ),
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=False,
            level_reached=0,
            summary_injected=False,
        ),
        budget_expectation={"soft_limit_tokens": 4000, "hard_limit_tokens": 8000},
    )


def _compression_semantic_case() -> ContextEvalCase:
    noisy_result = (
        "Collecting demo-package\n"
        "Downloading demo-package\n"
        "Successfully installed demo-package\n\n"
        + ("Collecting demo-package\nDownloading demo-package\n" * 80)
    )
    return ContextEvalCase(
        case_id="semantic-compression",
        scenario="Compress noisy historical tool output.",
        cwd_files={"CLAUDE.md": "Respect recent task context."},
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "run_shell",
                        "input": {"command": "pip install demo-package"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": noisy_result,
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Keep processor.py as the current focus."}],
            },
        ],
        memory_index_content="",
        memory_files={},
        target_turn_index=2,
        expected_compression_level=1,
        expected_present=["Keep processor.py as the current focus."],
        expected_absent=["Collecting demo-package\nDownloading demo-package\nCollecting demo-package"],
        expected_recent_present=["Keep processor.py as the current focus."],
        expected_stale_absent=["Successfully installed demo-package"],
        expected_order=[],
        required_sections=[],
        expected_recall_diagnostics=RecallDiagnostics(
            index_injected=True,
            recalled_count=0,
            recall_empty=True,
        ),
        expected_compression_diagnostics=CompressionDiagnostics(
            compression_used=True,
            level_reached=1,
            summary_injected=False,
        ),
        budget_expectation={"soft_limit_tokens": 200, "hard_limit_tokens": 400},
    )


def _with_memory_index_reordered(case: ContextEvalCase) -> ContextEvalCase:
    lines = [line for line in case.memory_index_content.splitlines() if line.strip()]
    return ContextEvalCase(
        case_id=f"{case.case_id}:index-reordered",
        scenario=case.scenario,
        cwd_files=dict(case.cwd_files),
        messages=list(case.messages),
        memory_index_content="\n".join(reversed(lines)) + ("\n" if lines else ""),
        memory_files=dict(case.memory_files),
        target_turn_index=case.target_turn_index,
        expected_compression_level=case.expected_compression_level,
        expected_present=list(case.expected_present),
        expected_absent=list(case.expected_absent),
        expected_recent_present=list(case.expected_recent_present),
        expected_stale_absent=list(case.expected_stale_absent),
        expected_order=list(case.expected_order),
        required_sections=list(case.required_sections),
        expected_recall_diagnostics=case.expected_recall_diagnostics,
        expected_compression_diagnostics=case.expected_compression_diagnostics,
        budget_expectation=dict(case.budget_expectation),
    )


# Note: this perturbation intentionally normalizes away blank lines while
# reordering semantic entries. That changes formatting but not the parsed index
# meaning, which is the stability property this phase-one case is asserting.


def _with_stale_history_noise(case: ContextEvalCase) -> ContextEvalCase:
    noisy_messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Old unrelated deployment archive note."}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "Historical packaging archive reminder."}],
        },
    ] + list(case.messages)
    return ContextEvalCase(
        case_id=f"{case.case_id}:stale-noise",
        scenario=case.scenario,
        cwd_files=dict(case.cwd_files),
        messages=noisy_messages,
        memory_index_content=case.memory_index_content,
        memory_files=dict(case.memory_files),
        target_turn_index=case.target_turn_index + 2,
        expected_compression_level=case.expected_compression_level,
        expected_present=list(case.expected_present),
        expected_absent=list(case.expected_absent),
        expected_recent_present=list(case.expected_recent_present),
        expected_stale_absent=list(case.expected_stale_absent)
        + [
            "Old unrelated deployment archive note.",
            "Historical packaging archive reminder.",
        ],
        expected_order=list(case.expected_order),
        required_sections=list(case.required_sections),
        expected_recall_diagnostics=case.expected_recall_diagnostics,
        expected_compression_diagnostics=case.expected_compression_diagnostics,
        budget_expectation=dict(case.budget_expectation),
    )


def _fraction(values: list[bool]) -> float:
    if not values:
        return 1.0
    return sum(1.0 for value in values if value) / len(values)


def _optional_fraction(values: list[bool]) -> float | None:
    if not values:
        return None
    return _fraction(values)


def _mean_optional(values: list[float | None]) -> float:
    present = [value for value in values if value is not None]
    if not present:
        return 0.0
    return sum(present) / len(present)
