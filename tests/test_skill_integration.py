"""Integration tests for the multi-phase skill system."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from xxcode.agent import StreamEvent
from xxcode.agent.loop import CoreExecutionEngine
from xxcode.agent.query_engine import QueryEngine
from xxcode.agent.state import AgentState
from xxcode.config import Config
from xxcode.skills.models import SkillFrontmatter, SkillSource, SkillSpec
from xxcode.tools import ToolCall
from xxcode.tools.file_read import ReadFileTool
from xxcode.tools.file_write import WriteFileTool
from xxcode.tools.registry import ToolRegistry
from xxcode.ui.repl import run_repl
from xxcode.ui.session import SessionStore


def _make_config(
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    api_max_tokens: int = 512,
) -> Config:
    return Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_max_tokens=api_max_tokens,
        auto_memory_enabled=False,
        mcp_enabled=False,
        cwd=(cwd or tmp_path),
        session_dir=tmp_path / "sessions",
        user_skills_dir=str(tmp_path / "user-skills"),
        skills_dir=".xxcode/skills",
        skills_enabled=True,
    )


def _write_skill(
    tmp_path: Path,
    name: str,
    frontmatter: str,
    body: str,
    *,
    source: SkillSource = SkillSource.PROJECT,
) -> Path:
    root = (
        tmp_path / ".xxcode" / "skills"
        if source == SkillSource.PROJECT
        else tmp_path / "user-skills"
    )
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_file


def _text_content(message: dict) -> str:
    return "\n".join(
        block.get("text", "")
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _find_text_message_index(messages: list[dict], needle: str) -> int:
    for index, message in enumerate(messages):
        if needle in _text_content(message):
            return index
    return -1


def _find_tool_result_message_index(messages: list[dict], tool_use_id: str) -> int:
    for index, message in enumerate(messages):
        for block in message.get("content", []):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                return index
    return -1


class _CaptureTextClient:
    def __init__(self, text: str = "done"):
        self.text = text
        self.calls = 0
        self.messages_by_call: list[list[dict]] = []
        self.tools_by_call: list[list[dict]] = []

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.messages_by_call.append(copy.deepcopy(messages))
        self.tools_by_call.append(copy.deepcopy(tools))
        yield {"type": "message_id", "id": f"msg-{self.calls}"}
        if self.text:
            yield {"type": "text_delta", "text": self.text}
        yield {"type": "usage", "input_tokens": 1, "output_tokens": 1}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _SkillToolClient:
    def __init__(self, *, skill: str, args: str = "", final_text: str = "done"):
        self.skill = skill
        self.args = args
        self.final_text = final_text
        self.calls = 0
        self.messages_by_call: list[list[dict]] = []
        self.tools_by_call: list[list[dict]] = []

    async def stream_chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.messages_by_call.append(copy.deepcopy(messages))
        self.tools_by_call.append(copy.deepcopy(tools))

        if self.calls == 1:
            yield {"type": "message_id", "id": "msg-skill-1"}
            yield {
                "type": "tool_use",
                "id": "tool-skill-1",
                "name": "Skill",
                "input": {"skill": self.skill, "args": self.args},
            }
            yield {"type": "usage", "input_tokens": 3, "output_tokens": 2}
            yield {"type": "stop_reason", "stop_reason": "tool_use"}
            return

        yield {"type": "message_id", "id": "msg-skill-2"}
        yield {"type": "text_delta", "text": self.final_text}
        yield {"type": "usage", "input_tokens": 2, "output_tokens": 2}
        yield {"type": "stop_reason", "stop_reason": "end_turn"}


class _FakeConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(arg) for arg in args))

    def clear(self):
        self.lines.append("[clear]")


class _FakeReplUI:
    def __init__(self, inputs: list[str]):
        self.console = _FakeConsole()
        self._inputs = iter(inputs)
        self.events: list[StreamEvent] = []
        self.exec_contexts: list[dict[str, object]] = []
        self.registry = None
        self.frames = []
        self.modals = []
        self.permission_answers = []
        self.update_count = 0

    def render_welcome(self, session_id=None, skill_registry=None) -> None:
        return None

    def set_registry(self, registry) -> None:
        self.registry = registry

    def set_exec_context(self, context: dict[str, object]) -> None:
        self.exec_contexts.append(dict(context))

    async def get_input(self, state=None) -> str | None:
        return next(self._inputs)

    def reset_for_new_session(self) -> None:
        return None

    async def ask_permission(self, tc, dangerous: bool = False) -> str:
        if self.permission_answers:
            return self.permission_answers.pop(0)
        pytest.fail(f"Unexpected permission prompt: {tc}")

    def render_event(self, event: StreamEvent) -> None:
        self.events.append(event)

    def mount(self, initial_frame) -> None:
        self.frames.append(initial_frame)

    def update(self, frame) -> None:
        self.frames.append(frame)
        self.update_count += 1

    def show_modal(self, modal_state) -> None:
        self.modals.append(modal_state)

    def clear_modal(self) -> None:
        self.modals.append(None)

    def shutdown(self, final_snapshot) -> None:
        self.frames.append(final_snapshot)


class _ContractAwareUi:
    uses_frame_transcript = False

    def __init__(self):
        self.prepared = 0
        self.rendered_events: list[str] = []
        self.mounted = []
        self.updated = []
        self.modals = []
        self.shutdown_frames = []

    async def prepare_runtime(self) -> None:
        self.prepared += 1

    async def ask_permission(self, tc, dangerous: bool = False) -> str:
        return "no"

    def render_event(self, event: StreamEvent) -> None:
        self.rendered_events.append(event.type)

    def mount(self, initial_frame) -> None:
        self.mounted.append(initial_frame)

    def update(self, frame) -> None:
        self.updated.append(frame)

    def show_modal(self, modal_state) -> None:
        self.modals.append(modal_state)

    def clear_modal(self) -> None:
        self.modals.append(None)

    def shutdown(self, final_snapshot) -> None:
        self.shutdown_frames.append(final_snapshot)


async def _collect_submit_events(
    engine: QueryEngine,
    prompt: str,
    *,
    state: AgentState | None = None,
    skill_permission_grant: bool | None = None,
) -> list:
    events = []
    async for event in engine.submit_message(prompt, state):
        events.append(event)
        if event.type != "permission_needed":
            continue
        assert skill_permission_grant is not None
        assert event.metadata.get("skill_shell_request") is not None
        engine.resolve_skill_permission(skill_permission_grant)
    return events


async def _collect_core_events(
    engine: CoreExecutionEngine,
    state: AgentState,
) -> list:
    events = []
    async for event in engine._query_loop(state):
        events.append(event)
        if event.type == "permission_needed":
            pytest.fail(f"Unexpected permission prompt: {event.metadata}")
    return events


def _make_query_engine(
    tmp_path: Path,
    *,
    client=None,
    **config_overrides,
) -> QueryEngine:
    engine = QueryEngine(_make_config(tmp_path, **config_overrides))
    if client is not None:
        engine.core_engine._build_client = lambda max_tokens=None: client
    return engine


def _submit_events(
    engine: QueryEngine,
    prompt: str,
    *,
    state: AgentState | None = None,
    skill_permission_grant: bool | None = None,
):
    return asyncio.run(
        _collect_submit_events(
            engine,
            prompt,
            state=state,
            skill_permission_grant=skill_permission_grant,
        )
    )


def _core_events(engine: QueryEngine, state: AgentState):
    return asyncio.run(_collect_core_events(engine.core_engine, state))


def _single_user_state(text: str, *, last_query: str | None = None, turn_count: int = 0):
    return AgentState(
        system_prompt="system",
        last_query=last_query,
        messages=[
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        turn_count=turn_count,
    )


def test_manual_project_skill_shell_approval_streams_in_same_submit_turn(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        "description: Review the current changes.\n",
        "Prompt says: !`echo hello`",
        source=SkillSource.PROJECT,
    )
    client = _CaptureTextClient(text="reviewed")
    engine = _make_query_engine(tmp_path, client=client)

    events = _submit_events(
        engine,
        "/review src/app.py",
        skill_permission_grant=True,
    )

    permission_index = next(
        index for index, event in enumerate(events) if event.type == "permission_needed"
    )
    text_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "text" and "reviewed" in event.content
    )
    assert permission_index < text_index
    assert client.calls == 1
    assert client.messages_by_call[0][-1]["content"][0]["text"] == (
        "Use skill 'review' with arguments: src/app.py"
    )
    assert _find_text_message_index(
        client.messages_by_call[0],
        "Skill 'review' (project skill) is active for this turn.",
    ) >= 0
    assert "/review src/app.py" not in str(client.messages_by_call[0])


def test_bundled_skill_is_available_without_local_skill_directories(tmp_path):
    client = _CaptureTextClient(text="bundled-ok")
    engine = _make_query_engine(tmp_path, client=client)

    events = _submit_events(engine, "/review")

    assert any(event.type == "text" and "bundled-ok" in event.content for event in events)
    assert _find_text_message_index(
        client.messages_by_call[0],
        "Skill 'review' (bundled skill) is active for this turn.",
    ) >= 0


def test_manual_project_skill_shell_denial_stops_before_model_request(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        "description: Review the current changes.\n",
        "Prompt says: !`echo hello`",
        source=SkillSource.PROJECT,
    )
    client = _CaptureTextClient(text="should not run")
    engine = _make_query_engine(tmp_path, client=client)

    events = _submit_events(
        engine,
        "/review",
        skill_permission_grant=False,
    )

    assert [event.type for event in events] == ["permission_needed", "error", "done"]
    assert "shell command was denied" in events[1].content
    assert client.calls == 0


def test_manual_skill_invocation_respects_paths_visibility(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        (
            "description: Review React components.\n"
            "paths:\n"
            "  - src/components/**\n"
        ),
        "Inspect the selected component.",
        source=SkillSource.PROJECT,
    )
    engine = _make_query_engine(tmp_path)

    events = _submit_events(engine, "/review")

    assert [event.type for event in events] == ["error", "done"]
    assert "Unknown command: /review" in events[0].content


def test_manual_skill_invocation_uses_runtime_context_cwd(tmp_path):
    _write_skill(
        tmp_path,
        "component-audit",
        (
            "description: Review React components.\n"
            "paths:\n"
            "  - src/components/**\n"
        ),
        "Inspect the selected component.",
        source=SkillSource.PROJECT,
    )
    visible_cwd = tmp_path / "src" / "components"
    visible_cwd.mkdir(parents=True)

    engine = _make_query_engine(tmp_path)
    engine.core_engine._context["cwd"] = str(visible_cwd)
    client = _CaptureTextClient(text="reviewed")
    engine.core_engine._build_client = lambda max_tokens=None: client

    events = _submit_events(engine, "/component-audit src/app.py")

    assert any(event.type == "text" and "reviewed" in event.content for event in events)
    assert _find_text_message_index(
        client.messages_by_call[0],
        "Skill 'component-audit' (project skill) is active for this turn.",
    ) >= 0


def test_listing_message_respects_runtime_context_cwd(tmp_path):
    _write_skill(
        tmp_path,
        "component-audit",
        (
            "description: Review React components.\n"
            "paths:\n"
            "  - src/components/**\n"
        ),
        "Inspect the selected component.",
        source=SkillSource.PROJECT,
    )
    visible_cwd = tmp_path / "src" / "components"
    visible_cwd.mkdir(parents=True)

    query_engine = _make_query_engine(tmp_path, api_max_tokens=4096)
    query_engine.core_engine._context["cwd"] = str(visible_cwd)
    client = _CaptureTextClient()
    query_engine.core_engine._build_client = lambda max_tokens=None: client
    state = _single_user_state("hello", last_query="hello")

    _core_events(query_engine, state)

    first_call_messages = client.messages_by_call[0]
    listing_index = _find_text_message_index(
        first_call_messages,
        "The following skills are available for use with the Skill tool:",
    )
    assert listing_index >= 0
    assert "- component-audit:" in _text_content(first_call_messages[listing_index])


def test_repl_manual_skill_invocation_uses_runtime_context_cwd(tmp_path):
    _write_skill(
        tmp_path,
        "component-audit",
        (
            "description: Review React components.\n"
            "paths:\n"
            "  - src/components/**\n"
        ),
        "Inspect the selected component.",
        source=SkillSource.PROJECT,
    )
    visible_cwd = tmp_path / "src" / "components"
    visible_cwd.mkdir(parents=True)

    config = _make_config(tmp_path)
    engine = _make_query_engine(tmp_path)
    engine.core_engine._context["cwd"] = str(visible_cwd)
    client = _CaptureTextClient(text="reviewed")
    engine.core_engine._build_client = lambda max_tokens=None: client
    ui = _FakeReplUI(["/component-audit src/app.py", "/quit"])

    asyncio.run(
        run_repl(
            engine,
            ui,
            config,
            skill_registry=engine.skill_registry,
        )
    )

    assert any(event.type == "text" and "reviewed" in event.content for event in ui.events)
    assert any(Path(ctx["cwd"]) == visible_cwd.resolve() for ctx in ui.exec_contexts)
    assert all("Unknown command" not in line for line in ui.console.lines)


def test_ui_runtime_updates_frames_for_task_snapshots(tmp_path):
    config = _make_config(tmp_path)
    engine = QueryEngine(config)
    ui = _FakeReplUI(["/quit"])

    runtime = engine.core_engine.task_runtime
    record = runtime.register_foreground_task(
        task_id="task-1",
        parent_task_id=None,
        parent_scope_id="main",
        worker_label="worker-1",
        description="demo task",
        agent_type="general-purpose",
    )

    from xxcode.ui.runtime import TaskUiEvent, UiRuntime

    ui_runtime = UiRuntime(engine=engine, ui=ui)
    ui_runtime.task_sink.emit(
        TaskUiEvent(
            type="task_snapshot_updated",
            task_id=record.task_id,
            record=record.to_dict(),
            summary="task started",
            result_text="",
        )
    )

    asyncio.run(ui_runtime._drain_task_events())

    assert ui.frames
    assert any("task-1" in frame.tasks for frame in ui.frames if hasattr(frame, "tasks"))


def test_ui_runtime_records_permission_audit(tmp_path):
    config = _make_config(tmp_path)
    engine = QueryEngine(config)
    ui = _FakeReplUI([])
    ui.permission_answers = ["no"]

    from xxcode.tools import ToolCall
    from xxcode.ui.runtime import UiEvent, UiRuntime

    runtime = UiRuntime(engine=engine, ui=ui)
    asyncio.run(
        runtime._handle_permission_event(
            UiEvent(
                type="permission_requested",
                content="write_file",
                metadata={
                    "tool_call": ToolCall(
                        id="tool-1",
                        name="write_file",
                        input={"file_path": str(tmp_path / "x.txt"), "content": "demo"},
                    ),
                    "risk": "high",
                    "dangerous": True,
                },
            )
        )
    )

    assert runtime.frame.permission_audit
    assert runtime.frame.permission_audit[-1]["tool_name"] == "write_file"
    assert runtime.frame.permission_audit[-1]["decision"] == "no"
    assert runtime.frame.permission_audit[-1]["risk_level"] == "high"
    assert ui.modals[0]["tool_name"] == "write_file"


def test_ui_runtime_coalesces_updates_before_flush(tmp_path):
    config = _make_config(tmp_path)
    engine = QueryEngine(config)
    ui = _FakeReplUI([])

    from xxcode.ui.runtime import UiEvent, UiRuntime

    runtime = UiRuntime(engine=engine, ui=ui)
    runtime._apply_ui_event(UiEvent(type="assistant_delta", content="a"))
    runtime._apply_ui_event(UiEvent(type="assistant_delta", content="b"))
    runtime._apply_ui_event(UiEvent(type="thinking_delta", content="c"))

    assert runtime.frame.transcript_entries == [
        {"kind": "assistant", "text": "ab"},
        {"kind": "thinking", "text": "c"},
    ]
    assert ui.update_count == 0

    asyncio.run(runtime._flush(force=True))

    assert ui.update_count == 1


def test_ui_runtime_routes_cost_and_error_without_warning_pollution(tmp_path):
    config = _make_config(tmp_path)
    engine = QueryEngine(config)
    ui = _FakeReplUI([])

    from xxcode.ui.runtime import UiEvent, UiRuntime

    runtime = UiRuntime(engine=engine, ui=ui)
    runtime._apply_ui_event(
        UiEvent(
            type="session_cost_updated",
            content="$0.1234",
            metadata={"cost": 0.1234},
        )
    )
    runtime._apply_ui_event(UiEvent(type="ui_fatal", content="boom"))

    assert runtime.frame.session_cost_text == "$0.1234"
    assert runtime.frame.session_cost_value == pytest.approx(0.1234)
    assert runtime.frame.warnings == []
    assert runtime.frame.errors == ["boom"]
    assert runtime.frame.transcript_entries[-1] == {"kind": "error", "text": "boom"}


def test_ui_runtime_uses_optional_backend_prepare_and_legacy_render_hooks(tmp_path):
    config = _make_config(tmp_path)

    class _FakeTaskRuntime:
        def set_task_event_sink(self, sink):
            self.sink = sink

    class _FakeCoreEngine:
        def __init__(self):
            self.task_runtime = _FakeTaskRuntime()

    class _FakeEngine:
        def __init__(self):
            self.core_engine = _FakeCoreEngine()

        async def submit_message(self, user_input, state=None, *, session_id=None):
            yield StreamEvent(type="text", content="hello")
            yield StreamEvent(type="done", content="")

    from xxcode.ui.runtime import UiRuntime

    ui = _ContractAwareUi()
    runtime = UiRuntime(engine=_FakeEngine(), ui=ui)
    asyncio.run(
        runtime.run_submit_message(
            user_input="hello",
            state_to_pass=None,
            session_id="sess-1",
        )
    )

    assert ui.prepared == 1
    assert len(ui.updated) == 1
    assert ui.rendered_events == ["text", "done"]


def test_repl_reports_save_failures_without_crashing_session(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    engine = QueryEngine(config)
    client = _CaptureTextClient(text="reviewed")
    engine.core_engine._build_client = lambda max_tokens=None: client
    ui = _FakeReplUI(["hello", "/quit"])

    from xxcode.ui.session import SessionStore

    class _FailingStore(SessionStore):
        def save(self, session_id, messages, meta=None, turn_count=None) -> None:
            raise OSError("disk full")

    monkeypatch.setattr("xxcode.ui.session.SessionStore", _FailingStore)

    asyncio.run(
        run_repl(
            engine,
            ui,
            config,
            skill_registry=engine.skill_registry,
        )
    )

    assert any(event.type == "text" and "reviewed" in event.content for event in ui.events)
    assert any("Session persistence error:" in line for line in ui.console.lines)
    assert any("disk full" in line for line in ui.console.lines)


def test_listing_message_is_injected_before_user_turn_and_skill_schema_is_registered(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        "description: Review current code changes.\n",
        "Review prompt.",
        source=SkillSource.USER,
    )
    _write_skill(
        tmp_path,
        "commit",
        "description: Create a commit message from the staged diff.\n",
        "Commit prompt.",
        source=SkillSource.PROJECT,
    )
    query_engine = _make_query_engine(tmp_path, api_max_tokens=32)
    client = _CaptureTextClient()
    query_engine.core_engine._build_client = lambda max_tokens=None: client
    state = _single_user_state("hello", last_query="hello")

    _core_events(query_engine, state)

    first_call_messages = client.messages_by_call[0]
    listing_index = _find_text_message_index(
        first_call_messages,
        "The following skills are available for use with the Skill tool:",
    )
    assert listing_index >= 0
    assert listing_index < len(first_call_messages) - 1
    assert first_call_messages[-1]["content"][0]["text"] == "hello"
    assert any(tool["name"] == "Skill" for tool in client.tools_by_call[0])


def test_skill_tool_rejects_disable_model_invocation(tmp_path):
    _write_skill(
        tmp_path,
        "private-review",
        (
            "description: Hidden from automatic model invocation.\n"
            "disable-model-invocation: true\n"
        ),
        "Do not auto-run this.",
        source=SkillSource.USER,
    )
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)

    result = asyncio.run(
        query_engine.core_engine._registry.execute(
            ToolCall(
                id="tool-skill-disabled",
                name="Skill",
                input={"skill": "private-review", "args": ""},
            ),
            {
                "cwd": str(tmp_path),
                "_registry": query_engine.core_engine._registry,
            },
        )
    )

    assert result.is_error
    assert "cannot be invoked automatically" in result.content


def test_model_invoked_inline_skill_is_deferred_until_after_tool_result_pairing(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        (
            "description: Review current code changes.\n"
            "allowed-tools:\n"
            "  - read_file\n"
        ),
        "Skill prompt for $ARGUMENTS",
        source=SkillSource.USER,
    )
    client = _SkillToolClient(skill="review", args="src/app.py", final_text="done")
    query_engine = _make_query_engine(tmp_path, client=client)
    state = _single_user_state(
        "please review this change",
        last_query="please review this change",
    )

    events = _core_events(query_engine, state)

    assert client.calls == 2
    assert not any(event.type == "permission_needed" for event in events)
    second_call_messages = client.messages_by_call[1]
    tool_result_index = _find_tool_result_message_index(second_call_messages, "tool-skill-1")
    skill_message_index = _find_text_message_index(
        second_call_messages,
        "Skill 'review' (user skill) is active for this turn.",
    )
    assert tool_result_index >= 0
    assert skill_message_index > tool_result_index
    assert "Skill prompt for src/app.py" in _text_content(second_call_messages[skill_message_index])
    assert [tool["name"] for tool in client.tools_by_call[1]] == ["read_file"]
    assert "review" in (
        query_engine.core_engine._skill_persistence.build_recovery_attachment("main") or ""
    )


def test_manual_inline_skill_restricts_tool_schemas_for_that_turn(tmp_path):
    _write_skill(
        tmp_path,
        "review",
        (
            "description: Review current code changes.\n"
            "allowed-tools:\n"
            "  - read_file\n"
        ),
        "Review only with $ARGUMENTS",
        source=SkillSource.USER,
    )
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)
    client = _CaptureTextClient(text="done")
    query_engine.core_engine._build_client = lambda max_tokens=None: client

    asyncio.run(_collect_submit_events(query_engine, "/review src/app.py"))

    assert [tool["name"] for tool in client.tools_by_call[0]] == ["read_file"]


def test_manual_fork_skill_returns_subagent_result_without_main_model_turn(
    tmp_path,
    monkeypatch,
):
    _write_skill(
        tmp_path,
        "audit",
        (
            "description: Inspect repository state in a fork.\n"
            "context: fork\n"
            "effort: quick\n"
        ),
        "Fork prompt body.",
        source=SkillSource.USER,
    )
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)
    client = _CaptureTextClient(text="should not run")
    query_engine.core_engine._build_client = lambda max_tokens=None: client

    captured: dict[str, object] = {}

    class _DummySubAgent:
        @property
        def tokens_used(self) -> tuple[int, int]:
            return (7, 4)

        async def run(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "fork complete"

    def _fake_create_subagent(**kwargs):
        captured["tool_names"] = sorted(
            tool.name for tool in kwargs["registry"].list_tools()
        )
        return _DummySubAgent()

    monkeypatch.setattr(
        query_engine.skill_executor,
        "_create_subagent",
        _fake_create_subagent,
    )

    events = asyncio.run(_collect_submit_events(query_engine, "/audit"))

    assert [event.type for event in events] == ["text", "cost", "done"]
    assert events[0].content == "fork complete"
    assert events[1].metadata is not None
    assert events[1].metadata["cost"] > 0
    assert client.calls == 0
    assert "read_file" in captured["tool_names"]
    assert "write_file" not in captured["tool_names"]

    state = query_engine._last_state
    assert state is not None
    assert state.turn_count == 1
    assert state.user_turn_count == 1
    assert state.total_input_tokens == 7
    assert state.total_output_tokens == 4
    assert query_engine._session_cost > 0
    assert state.messages[-2]["role"] == "user"
    assert state.messages[-2]["content"][0]["text"] == "Use skill 'audit'."
    assert state.messages[-1]["role"] == "assistant"
    assert _text_content(state.messages[-1]) == "fork complete"
    assert not any(
        message.get("role") == "user" and _text_content(message) == "fork complete"
        for message in state.messages
    )


def test_fork_skill_uses_subagent_with_default_read_only_tool_pool(tmp_path, monkeypatch):
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)
    skill = SkillSpec(
        frontmatter=SkillFrontmatter(
            name="audit",
            description="Inspect repository state in a fork.",
            context="fork",
            effort="quick",
        ),
        source=SkillSource.USER,
        directory=tmp_path / "user-skills" / "audit",
        skill_file=None,
        canonical_name="audit",
        content="Fork prompt body.",
    )

    base_registry = ToolRegistry([ReadFileTool(), WriteFileTool()])
    captured: dict[str, object] = {}

    class _DummySubAgent:
        async def run(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "fork complete"

    def _fake_create_subagent(**kwargs):
        captured["tool_names"] = sorted(tool.name for tool in kwargs["registry"].list_tools())
        captured["thinking_budget_tokens"] = kwargs["thinking_budget_tokens"]
        return _DummySubAgent()

    monkeypatch.setattr(query_engine.skill_executor, "_create_subagent", _fake_create_subagent)

    async def _approve(_request) -> bool:
        return True

    result = asyncio.run(
        query_engine.skill_executor.execute(
            skill,
            "",
            session_id="sess-123",
            approve_project_shell=_approve,
            base_registry=base_registry,
            parent_state=None,
            extra_context={},
        )
    )

    assert result.mode == "fork"
    assert result.result_text == "fork complete"
    assert "Reasoning effort: quick." in captured["prompt"]
    assert "Base directory for this skill: " + str(skill.directory) in captured["prompt"]
    assert captured["prompt"].endswith("Fork prompt body.")
    assert captured["thinking_budget_tokens"] == 1024
    assert captured["tool_names"] == ["read_file"]


def test_recovery_attachment_is_not_injected_before_compaction(tmp_path):
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)
    client = _CaptureTextClient()
    query_engine.core_engine._build_client = lambda max_tokens=None: client
    query_engine.core_engine.record_skill_invocation(
        "review",
        str(tmp_path / ".xxcode" / "skills" / "review" / "SKILL.md"),
        "Recovered skill prompt",
        turn_count=2,
    )
    state = AgentState(
        system_prompt="system",
        last_query="continue",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        ],
    )

    asyncio.run(_collect_core_events(query_engine.core_engine, state))

    recovery_index = _find_text_message_index(
        client.messages_by_call[0],
        "The following skills were previously invoked and may still be relevant:",
    )
    assert recovery_index == -1


def test_recovery_attachment_is_injected_after_manual_compaction(tmp_path):
    config = _make_config(tmp_path)
    config.context_compress_threshold = 0.0
    query_engine = QueryEngine(config)
    client = _CaptureTextClient()
    query_engine.core_engine._build_client = lambda max_tokens=None: client
    query_engine.core_engine.record_skill_invocation(
        "review",
        str(tmp_path / ".xxcode" / "skills" / "review" / "SKILL.md"),
        "Recovered skill prompt",
        turn_count=5,
    )
    state = AgentState(
        system_prompt="system",
        last_query="continue",
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "continue " * 200}],
            },
        ],
    )

    state = asyncio.run(query_engine._compact(state))
    asyncio.run(_collect_core_events(query_engine.core_engine, state))

    recovery_index = _find_text_message_index(
        client.messages_by_call[0],
        "The following skills were previously invoked and may still be relevant:",
    )
    assert recovery_index >= 0
    assert "Recovered skill prompt" in _text_content(client.messages_by_call[0][recovery_index])


def test_recovery_snapshot_survives_restart_but_waits_for_compaction(tmp_path):
    config = _make_config(tmp_path)
    store = SessionStore(config.session_dir)

    first_engine = QueryEngine(config)
    first_engine.core_engine.record_skill_invocation(
        "review",
        str(tmp_path / ".xxcode" / "skills" / "review" / "SKILL.md"),
        "Recovered skill prompt",
        turn_count=4,
    )
    state = AgentState(
        system_prompt="system",
        last_query="continue",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        ],
        turn_count=4,
    )
    store.save_state_with_recovery(
        "session-restore",
        state,
        first_engine.core_engine.export_skill_recovery_snapshot(),
    )

    second_engine = QueryEngine(config)
    second_engine.core_engine.import_skill_recovery_snapshot(
        store.load_skill_recovery("session-restore")
    )
    loaded_state = store.load_state("session-restore")
    assert loaded_state is not None

    client = _CaptureTextClient()
    second_engine.core_engine._build_client = lambda max_tokens=None: client

    asyncio.run(_collect_core_events(second_engine.core_engine, loaded_state))
    assert _find_text_message_index(
        client.messages_by_call[0],
        "The following skills were previously invoked and may still be relevant:",
    ) == -1

    second_engine.core_engine.mark_skill_history_compacted("main")
    asyncio.run(_collect_core_events(second_engine.core_engine, loaded_state))
    assert _find_text_message_index(
        client.messages_by_call[1],
        "The following skills were previously invoked and may still be relevant:",
    ) >= 0


def test_previous_inline_skill_message_is_stripped_before_next_user_turn(tmp_path):
    config = _make_config(tmp_path)
    query_engine = QueryEngine(config)
    client = _CaptureTextClient(text="ok")
    query_engine.core_engine._build_client = lambda max_tokens=None: client

    asyncio.run(_collect_submit_events(query_engine, "/review src/app.py"))
    state = query_engine._last_state
    assert state is not None

    asyncio.run(_collect_submit_events(query_engine, "follow up question", state=state))

    assert len(client.messages_by_call) == 2
    assert _find_text_message_index(
        client.messages_by_call[1],
        "Skill 'review' (bundled skill) is active for this turn.",
    ) == -1
