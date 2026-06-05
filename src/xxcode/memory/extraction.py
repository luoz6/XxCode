"""Background memory extraction agent.

After the main agent finishes a turn without tool calls, a lightweight
sub-agent is forked to scan the conversation and extract persistent
memories.  Three controls prevent noise and duplication:

1. **Throttling** — extraction fires at most every N turns.
2. **Mutual exclusion** — if the main agent already wrote to the memory
   directory this turn, extraction is skipped.
3. **Concurrency protection** — if an extraction is already in progress,
   the new context is stored as *pending* and a trailing run is launched
   when the current one finishes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.definitions import AgentDef, build_filtered_registry
from ..agent.state import AgentState
from ..config import Config
from .index import load_memory_index, write_memory_index

logger = logging.getLogger(__name__)

# ── Module-level constants ─────────────────────────────────────────

_EXTRACTION_AGENT_DEF = AgentDef(
    name="auto-memory-extract",
    description=(
        "Background agent that extracts persistent memories "
        "from conversation context"
    ),
    tools_allowlist={"read_file", "write_file", "edit_file",
                     "grep_search", "glob_match", "run_shell"},
    tools_denylist=None,
    model=None,
    is_read_only=False,
    max_turns=5,
    permission_mode="inherit",
)

# ── Extraction system prompt ──────────────────────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_EXTRACTION_PROMPT_FILE = _PROMPTS_DIR / "extraction_system.md"

_EXTRACTION_SYSTEM_PROMPT_FALLBACK = """\
You are a background memory extraction agent. Your job is to identify \
useful, reusable information from the recent conversation and save it \
as memory files in the memory directory.

## What to extract
- **user** — User role, preferences, knowledge, coding conventions.
  Save discoveries about how the user works.
- **feedback** — Behavioral corrections from the user. Include **Why:** \
and **How to apply:** lines. Also capture confirmations of non-obvious \
approaches that worked.
- **project** — Ongoing work, decisions, deadlines. Convert relative \
dates to absolute dates.
- **reference** — Pointers to external systems (issue trackers, \
dashboards, docs).

## What NOT to extract
- Code patterns, architecture, file paths — these are in the code
- Git history — use git log / git blame
- Debugging solutions — the fix is in the code
- Content already in XXCODE.md files
- Ephemeral task details or in-progress work
- Trivial one-off facts

## Strategy (2 turns typical, 5 max)
Turn 1: Use the provided manifest and read only existing memory files that \
overlap with potential new memories. Check for duplicates.
Turn 2+: Write new memory files for genuinely new information. \
Update existing files only if adding significant new detail.

## File format
Each memory file is Markdown with YAML frontmatter:

---
name: {{short-kebab-case-slug}}
description: {{one-line summary}}
metadata:
  type: {{user|feedback|project|reference}}
---

{{content}}

After creating or updating memory files, update MEMORY.md so it remains a \
compact index of all available memory files."""


def _load_extraction_system_prompt(custom_path: Path | None = None) -> str:
    """Load the extraction system prompt from file, with fallback.

    Priority:
    1. custom_path (if provided and exists)
    2. Default prompts/extraction_system.md alongside this module
    3. Hardcoded fallback string
    """
    paths_to_try = []
    if custom_path:
        paths_to_try.append(custom_path)
    paths_to_try.append(_EXTRACTION_PROMPT_FILE)

    for path in paths_to_try:
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except OSError:
            continue

    return _EXTRACTION_SYSTEM_PROMPT_FALLBACK


# ── Extraction config ─────────────────────────────────────────────


@dataclass
class ExtractionConfig:
    """Controls when and how extraction runs."""

    turns_between_extractions: int = 5
    max_extraction_turns: int = 5
    context_message_count: int = 8
    custom_prompt_path: Path | None = None


_DEFAULT_CONFIG = ExtractionConfig()


# ── Registry builder ──────────────────────────────────────────────


def build_extraction_registry(base_registry):
    """Build a filtered ToolRegistry containing only extraction-safe tools.

    Write tools are restricted via ``allowed_write_roots`` in the execution
    context (enforced in ``validate_input``). The ``run_shell`` tool is included
    for read-only commands like ``ls`` and ``cat`` — the extraction prompt
    instructs the model to only use safe, read-only shell commands.
    """
    return build_filtered_registry(base_registry, _EXTRACTION_AGENT_DEF)


# ── Prompt builder ────────────────────────────────────────────────


def build_extraction_prompt(
    messages_slice: list[dict[str, Any]],
    existing_manifest: str,
    *,
    max_turns: int = 5,
) -> str:
    """Build the extraction sub-agent user prompt.

    Args:
        messages_slice: The last N messages from the parent conversation.
        existing_manifest: Human-readable manifest of existing memories.
        max_turns: Hard turn limit for the extraction agent.
    """
    # Format messages as readable text
    msg_lines: list[str] = []
    for msg in messages_slice:
        role = msg.get("role", "unknown")
        content = msg.get("content", [])
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    text_parts.append(
                        f"[tool: {block.get('name', '?')} "
                        f"id={block.get('id', '?')}]"
                    )
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    text_parts.append(
                        f"[tool_result: {c[:200]}"
                        f"{'...(truncated)' if len(c) > 200 else ''}]"
                    )
            msg_lines.append(f"## {role}\n" + "\n".join(text_parts))
        else:
            msg_lines.append(f"## {role}\n{content}")

    conversation_text = "\n\n".join(msg_lines)

    return f"""## Existing MEMORY.md index (check before creating duplicates)

{existing_manifest}

## Recent conversation

{conversation_text}

## Instructions

1. Use the existing MEMORY.md index above to avoid duplicate memories.
2. Read any existing memory files that overlap with new insights.
3. Write NEW memory files only for genuinely new, reusable information.
4. Do NOT write information already in existing memory files.
5. Keep MEMORY.md in sync with any memory files you create or update.

Be selective — only extract insights that would help in a FUTURE,
unrelated coding session. Skip trivial facts and ephemeral details.

You have {max_turns} turns maximum. Aim for 2 turns:
Turn 1: parallel reads. Turn 2: parallel writes.
Return a brief summary of what you saved (or why you saved nothing)."""


# ── Extraction controller ─────────────────────────────────────────


class ExtractionController:
    """Manages the background memory extraction lifecycle.

    Owned by ``CoreExecutionEngine``.  Handles throttling, mutual
    exclusion, and the pending-context / trailing-run concurrency pattern.

    Usage::

        controller = ExtractionController(config, base_registry)
        ...
        if not turn.tool_calls:
            controller.schedule(state, memory_dir)
    """

    def __init__(self, config: Config, base_registry, extraction_config: ExtractionConfig | None = None):
        self._config = config
        self._base_registry = base_registry
        self._extraction_config = extraction_config or _DEFAULT_CONFIG
        self._current_task: asyncio.Task | None = None
        self._pending_context: dict[str, Any] | None = None
        self._last_result: str | None = None

    # ── Public API ────────────────────────────────────────────────

    def should_extract(self, state: AgentState) -> bool:
        """Return True if extraction should fire this turn.

        Checks throttling (turn spacing) and mutual exclusion
        (main agent already wrote memories).
        """
        cfg = getattr(self, "_extraction_config", _DEFAULT_CONFIG)
        current_user_turn = getattr(state, "user_turn_count", 0)
        last_user_turn = getattr(state, "last_extraction_user_turn", 0)
        turns_since = current_user_turn - last_user_turn
        if turns_since < cfg.turns_between_extractions:
            logger.debug(
                "Skipping memory extraction: turns since last extraction (%d) is below the threshold (%d).",
                turns_since,
                cfg.turns_between_extractions,
            )
            return False
        if state.memory_writes_since_extraction:
            logger.debug(
                "Skipping memory extraction: the main agent wrote memory during this turn.",
            )
            return False
        return True

    def schedule(
        self, state: AgentState, memory_dir: Path,
    ) -> asyncio.Task | None:
        """Launch a background extraction or store pending context.

        Returns the created Task if launched, None if throttled or
        the context was stored as pending.
        """
        if not self.should_extract(state):
            return None

        # Build the context payload for extraction
        recent = state.messages[-self._extraction_config.context_message_count:]
        ctx_payload = {
            "messages": recent,
            "turn_count": state.turn_count,
            "user_turn_count": getattr(state, "user_turn_count", 0),
        }

        if self._current_task is not None and not self._current_task.done():
            # Extraction already in progress — store for trailing run
            self._pending_context = ctx_payload
            logger.debug("Extraction in progress — pending context stored")
            return None

        # Clean up finished task
        if self._current_task is not None and self._current_task.done():
            self._current_task = None

        self._current_task = asyncio.create_task(
            self._run_extraction(
                recent_messages=recent,
                state=state,
                memory_dir=memory_dir,
            )
        )
        return self._current_task

    def has_pending_result(self) -> bool:
        """Return True if a completed extraction result is available."""
        return self._last_result is not None

    def consume_result(self) -> str | None:
        """Return and clear the last completed extraction result."""
        result = self._last_result
        self._last_result = None
        return result

    def cancel(self) -> None:
        """Cancel any in-progress extraction."""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            self._current_task = None
        self._pending_context = None
        self._last_result = None

    # ── Internal ─────────────────────────────────────────────────

    async def _run_extraction(
        self,
        recent_messages: list[dict[str, Any]],
        state: AgentState,
        memory_dir: Path,
    ) -> None:
        """Internal: scan existing memories, launch sub-agent, store result."""
        try:
            index_content = load_memory_index(memory_dir)
            manifest = index_content or "(no indexed memories yet)"

            # Build prompt
            prompt = build_extraction_prompt(
                messages_slice=recent_messages,
                existing_manifest=manifest,
                max_turns=self._extraction_config.max_extraction_turns,
            )

            # Load system prompt from file (with fallback)
            system_prompt = _load_extraction_system_prompt(
                self._extraction_config.custom_prompt_path
            )

            # Build filtered registry for extraction agent
            filtered_registry = build_extraction_registry(self._base_registry)

            # Prepare tool execution context with path restrictions
            extra_context = {
                "allowed_write_roots": [str(memory_dir.resolve())],
                "skip_read_before_edit": True,
            }

            # Create and run sub-agent
            from ..agent.subagent import SubAgent

            model_override = (
                self._config.extraction_model
                or self._config.api_model
            )

            sub = SubAgent(
                config=self._config,
                registry=filtered_registry,
                definition=_EXTRACTION_AGENT_DEF,
                parent_state=state,
                model_override=model_override,
                extra_context=extra_context,
                system_prompt_override=system_prompt,
            )

            result = await sub.run(prompt)
            write_memory_index(memory_dir)

            # Format result for injection
            self._last_result = _format_extraction_result(result)
            state.last_extraction_turn = state.turn_count
            state.last_extraction_user_turn = getattr(state, "user_turn_count", 0)
            state.memory_writes_since_extraction = False

            logger.info("Memory extraction completed: %s", result[:200])

        except Exception:
            logger.debug("Memory extraction failed", exc_info=True)
        finally:
            # Check for pending trailing run
            if self._pending_context is not None:
                pending = self._pending_context
                self._pending_context = None
                logger.debug("Starting trailing extraction run")
                self._current_task = asyncio.create_task(
                    self._run_extraction(
                        recent_messages=pending["messages"],
                        state=state,
                        memory_dir=memory_dir,
                    )
                )
            else:
                self._current_task = None


def _format_extraction_result(agent_result: str) -> str:
    """Format the extraction agent's output as a system-reminder."""
    summary = agent_result.strip()[:500]
    if not summary:
        return ""
    return (
        f'<system-reminder note="auto-memory">\n'
        f"Background memory extraction completed:\n"
        f"{summary}\n"
        f"</system-reminder>"
    )
