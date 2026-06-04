"""AgentTool - spawn a sub-agent to handle delegated tasks."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ...agent.definitions import build_filtered_registry, get_agent_definition
from ...agent.task_runtime import TERMINAL_TASK_STATUSES, build_subagent_state
from ...agent.subagent import SubAgent
from .. import Tool

logger = logging.getLogger(__name__)

MAX_BACKGROUND_WORKERS_PER_SCOPE = 32


class AgentInput(BaseModel):
    """Input schema for the Agent tool."""

    description: str = Field(
        description="A short description of the delegated task.",
    )
    prompt: str = Field(
        description="Detailed instructions for the sub-agent.",
    )
    subagent_type: str = Field(
        default="general-purpose",
        description=(
            "Agent type to spawn. Available types include "
            "'general-purpose', 'Explore', 'Plan', 'Coordinator', and "
            "'claude-code-guide'."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Optional model override. If omitted, inherit from parent config.",
    )
    run_in_background: bool = Field(
        default=False,
        description="If true, spawn the worker in background and return its task_id immediately.",
    )
    worker_label: str | None = Field(
        default=None,
        description="Optional human-readable worker label. task_id is still system-generated.",
    )
    reusable: bool = Field(
        default=False,
        description="If true, keep the background worker idle for SendMessage reuse after it finishes.",
    )
    isolation: str | None = Field(
        default=None,
        description="Filesystem isolation mode: null (shared filesystem) or 'worktree' (git worktree).",
    )


class AgentTool(Tool):
    """Spawn a sub-agent for complex, multi-step tasks."""

    name = "Agent"
    description = (
        "Launch a new agent to handle complex, multi-step tasks.\n\n"
        "Available agent types:\n"
        "- general-purpose: all tools.\n"
        "- Explore: read-only code search.\n"
        "- Plan: read-only implementation planning.\n"
        "- Coordinator: task orchestration only. Always use run_in_background=true when spawning workers. "
        "Never use sync mode. Use TaskWait to wait for workers. Never busy-poll with repeated TaskList calls. "
        "Use TaskList and TaskGet for inspection and result retrieval only. "
        "After all target workers settle, synthesize their results into one final answer. "
        "Workers are automatically isolated via git worktrees.\n"
        "- claude-code-guide: documentation lookup."
    )
    input_schema = AgentInput

    _is_read_only = False
    _is_concurrency_safe = True
    _is_destructive = False

    async def validate_input(
        self,
        input: AgentInput,
        context: dict[str, Any],
    ) -> tuple[bool, str]:
        if not input.description.strip():
            return False, "description must not be empty."
        if not input.prompt.strip():
            return False, "prompt must not be empty."
        return True, ""

    async def execute(
        self,
        input: AgentInput,
        context: dict[str, Any],
    ) -> str:
        config = context.get("config")
        if config is None:
            return "Error: AgentTool requires a Config in the execution context."

        base_registry = context.get("_registry")
        if base_registry is None:
            return "Error: No tool registry available in context."

        task_runtime = context.get("task_runtime")
        if task_runtime is None:
            return "Error: AgentTool requires task runtime support in the execution context."

        parent_state = context.get("parent_state")
        definition = get_agent_definition(input.subagent_type)
        model = input.model or definition.model or config.api_model
        filtered_registry = build_filtered_registry(base_registry, definition)
        if not filtered_registry.list_tools():
            return (
                f"Error: No tools available for agent type "
                f"'{input.subagent_type}'. Check the allowlist/denylist."
            )

        parent_scope_id = str(context.get("scope_id", "main") or "main")
        parent_task_id = context.get("current_task_id")
        caller_agent_type = str(context.get("current_agent_type", "") or "")
        worker_label = (input.worker_label or input.description).strip()

        # ── Worktree isolation ────────────────────────────────────
        isolation_mode = input.isolation or definition.isolation
        worktree_path: str | None = None
        if isolation_mode == "worktree":
            from ...agent.worktree import WorktreeManager

            repo_root = WorktreeManager.find_git_root(config.cwd)
            if repo_root is None:
                logger.warning(
                    "Worktree isolation requested but cwd is not in a git repo — "
                    "falling back to shared filesystem."
                )
            else:
                wt_result = await WorktreeManager.create(
                    repo_root,
                    base_ref=config.worktree_base_ref,
                    agent_type=input.subagent_type,
                    worktrees_dir=config.worktree_dir,
                )
                if wt_result.worktree_path is not None:
                    worktree_path = str(wt_result.worktree_path)

        if caller_agent_type == "Coordinator" and not input.run_in_background:
            return (
                "Error: Coordinator must always spawn workers with "
                "run_in_background=true. Sync mode is not allowed."
            )

        if input.run_in_background:
            sibling_tasks = task_runtime.list_tasks(parent_scope_id)
            if any(
                record.worker_label == worker_label
                and record.status not in TERMINAL_TASK_STATUSES
                for record in sibling_tasks
            ):
                return (
                    "Error: A worker with the same worker_label already exists in this scope. "
                    "Choose a unique worker_label."
                )
            active_siblings = [
                record
                for record in sibling_tasks
                if record.status not in TERMINAL_TASK_STATUSES
            ]
            if len(active_siblings) >= MAX_BACKGROUND_WORKERS_PER_SCOPE:
                return (
                    "Error: Too many active background workers in this scope. "
                    f"Limit is {MAX_BACKGROUND_WORKERS_PER_SCOPE}."
                )
            record = await task_runtime.spawn_worker(
                config=config,
                registry=filtered_registry,
                definition=definition,
                parent_state=parent_state,
                description=input.description,
                prompt=input.prompt,
                agent_type=input.subagent_type,
                model_override=model,
                worker_label=worker_label,
                reusable=input.reusable,
                parent_scope_id=parent_scope_id,
                parent_task_id=str(parent_task_id) if parent_task_id else None,
                extra_context={
                    **{
                        key: value
                        for key, value in context.items()
                        if key not in {"parent_state", "_registry"}
                    },
                    **({"worktree_cwd": worktree_path} if worktree_path is not None else {}),
                },
                worktree_path=worktree_path,
            )
            return (
                "Background worker launched.\n"
                f"task_id: {record.task_id}\n"
                f"worker_label: {record.worker_label}\n"
                f"status: {record.status}\n"
                f"reusable: {str(record.reusable).lower()}"
            )

        subagent_scope_id = f"scope-{uuid.uuid4().hex[:12]}"
        subagent_task_id = f"subagent-{input.subagent_type}-{uuid.uuid4().hex[:8]}"
        sync_record = task_runtime.register_foreground_task(
            task_id=subagent_task_id,
            parent_task_id=str(parent_task_id) if parent_task_id else None,
            parent_scope_id=parent_scope_id,
            worker_label=worker_label,
            description=input.description,
            agent_type=input.subagent_type,
        )
        task_runtime.ensure_scope(subagent_scope_id)

        extra_context = {
            **{
                key: value
                for key, value in context.items()
                if key not in {"parent_state", "_registry"}
            },
            "scope_id": subagent_scope_id,
            "current_task_id": subagent_task_id,
            "parent_scope_id": parent_scope_id,
            "parent_task_id": str(parent_task_id) if parent_task_id else None,
            "current_agent_type": input.subagent_type,
        }
        if worktree_path is not None:
            extra_context["worktree_cwd"] = worktree_path
        child_state = build_subagent_state(parent_state, definition.permission_mode)

        sub = SubAgent(
            config=config,
            registry=filtered_registry,
            definition=definition,
            parent_state=child_state,
            model_override=model,
            agent_type=input.subagent_type,
            extra_context=extra_context,
        )

        logger.info(
            "Spawning sub-agent type='%s' description='%s'",
            input.subagent_type,
            input.description,
        )
        cleanup_report = None
        try:
            result = await sub.run(input.prompt)
            task_runtime.complete_foreground_task(sync_record, termination_reason="completed")
        except Exception:
            task_runtime.fail_foreground_task(sync_record, termination_reason="failed")
            raise
        finally:
            cleanup_report = await task_runtime.cleanup_scope(subagent_scope_id)
            task_runtime.discard_foreground_task(subagent_task_id)
            if worktree_path is not None:
                from ...agent.worktree import WorktreeManager
                await WorktreeManager.remove(Path(worktree_path))

        in_tok, out_tok = sub.tokens_used
        if in_tok or out_tok:
            logger.info(
                "Sub-agent '%s' used %d input + %d output tokens.",
                input.subagent_type,
                in_tok,
                out_tok,
            )

        if cleanup_report.active_tasks_stopped:
            result = (
                "Sub-agent returned before its background child tasks settled. "
                f"Stopped and discarded {cleanup_report.active_tasks_stopped} background task(s).\n\n"
                f"Partial sub-agent output:\n{result}"
            )

        return (
            f"[Sub-agent: {input.subagent_type}]\n"
            f"[Task: {input.description}]\n"
            f"[Tokens: {in_tok} in / {out_tok} out]\n\n"
            f"{result}"
        )

    def render_tool_use(self, input: BaseModel) -> str:
        assert isinstance(input, AgentInput)
        sub_type = input.subagent_type
        desc = input.description
        model = input.model or ""
        bg = " [bg]" if input.run_in_background else ""
        iso = " [wt]" if input.isolation == "worktree" else ""
        if model:
            return f"Agent({sub_type}{bg}{iso}, model={model}): {desc}"
        return f"Agent({sub_type}{bg}{iso}): {desc}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        if is_error:
            return f"Agent failed: {content[:150]}"
        lines = [line for line in content.split("\n") if line.strip() and not line.startswith("[")]
        first = lines[0][:120] if lines else "Agent completed"
        return f"Agent done: {first}"
