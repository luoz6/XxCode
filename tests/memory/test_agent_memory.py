"""Tests for MEMORY.md-backed sub-agent memory."""

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

from xxcode.agent.subagent import SubAgent
from xxcode.memory.agent_memory import (
    build_recalled_agent_memories_message,
    build_agent_memory_context_messages,
    build_agent_memory_prompt,
    get_agent_memory_directories,
    recall_agent_memories_for_query,
    refresh_agent_memory_indexes,
    resolve_agent_memory_project_root,
    sanitize_agent_type_for_path,
)
from xxcode.memory.models import MemoryEntry
from xxcode.memory.store import MemoryStore
from xxcode.tools.registry import ToolRegistry


class _MockRecallClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.response_text


class _StreamingTestClient:
    def __init__(self, turns: list[list[dict]]):
        self.turns = turns
        self.calls = 0
        self.messages_by_call: list[list[dict]] = []

    async def stream_chat(self, system_prompt, messages, tools):
        self.messages_by_call.append(copy.deepcopy(messages))
        events = self.turns[self.calls]
        self.calls += 1
        for event in events:
            yield event


class _FakeReadTool:
    name = "read_file"
    description = "Read a file"
    input_schema = SimpleNamespace(model_validate=lambda payload: payload)
    aliases: list[str] = []
    deprecated_aliases: dict[str, str] = {}

    def is_enabled(self):
        return True

    def is_read_only(self, input=None):
        return True

    def to_api_schema(self):
        return {"name": self.name, "input_schema": {"type": "object"}}

    async def validate_input(self, input, context):
        return True, ""

    async def execute(self, input, context):
        return "src/app.py\nsrc/test_app.py"

    async def format_large_result(self, content, max_chars, tool_use_id="", session_dir=""):
        return content


def _patch_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _make_project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def _make_subagent_config(cwd: Path, tmp_path, **overrides):
    return SimpleNamespace(**{
        "cwd": cwd,
        "auto_memory_enabled": True,
        "api_model": "fake",
        "api_key": "fake",
        "api_base_url": "http://fake",
        "api_max_tokens": 1000,
        "max_tool_output_chars": 1000,
        "session_dir": tmp_path / "sessions",
        **overrides,
    })


def _make_definition(name="Explore", **overrides):
    return SimpleNamespace(**{
        "name": name,
        "description": "Read-only search agent.",
        "model": None,
        "max_turns": 5,
        **overrides,
    })


def _make_subagent(cwd: Path, tmp_path, *, registry=None, agent_type="Explore", **config_overrides):
    return SubAgent(
        config=_make_subagent_config(cwd, tmp_path, **config_overrides),
        registry=registry if registry is not None else ToolRegistry(),
        definition=_make_definition(name=agent_type),
        agent_type=agent_type,
    )


def _message_text(messages: list[dict]) -> str:
    return "\n".join(
        block["text"]
        for message in messages
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_sanitize_agent_type_for_path():
    assert sanitize_agent_type_for_path("explorer:repo") == "explorer-repo"
    assert sanitize_agent_type_for_path(" Plan Agent ") == "plan-agent"
    assert sanitize_agent_type_for_path("EXPLORE") == "explore"
    assert sanitize_agent_type_for_path("///") == "general-purpose"


def test_agent_memory_directories_use_three_scopes(tmp_path, monkeypatch):
    home = _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)

    dirs = get_agent_memory_directories("explorer:repo", project)

    assert [d.name for d in dirs] == ["user", "project", "local"]
    assert dirs[0].path == home / ".XxCode" / "agent-memory" / "explorer-repo"
    assert dirs[1].path == project / ".xxcode" / "agent-memory" / "explorer-repo"
    assert dirs[2].path == project / ".xxcode" / "agent-memory-local" / "explorer-repo"


def test_agent_memory_directories_use_git_root_for_project_scopes(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    subdir = repo / "nested" / "deep"
    subdir.mkdir(parents=True)
    (repo / ".git").mkdir()

    dirs = get_agent_memory_directories("Explore", subdir)

    assert dirs[1].path == repo / ".xxcode" / "agent-memory" / "explore"
    assert dirs[2].path == repo / ".xxcode" / "agent-memory-local" / "explore"


def test_resolve_agent_memory_project_root_prefers_git_root(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert resolve_agent_memory_project_root(nested) == repo.resolve()


def test_refresh_agent_memory_indexes_rewrites_memory_index(tmp_path, monkeypatch):
    home = _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    user_dir = home / ".XxCode" / "agent-memory" / "explore"
    local_dir = project / ".xxcode" / "agent-memory-local" / "explore"

    MemoryStore(user_dir).save_entry(MemoryEntry(
        name="repo-navigation",
        description="Use glob first, then grep.",
        metadata={"type": "reference"},
    ))
    MemoryStore(local_dir).save_entry(MemoryEntry(
        name="local-test-layout",
        description="Tests live under tests/",
        metadata={"type": "project"},
    ))
    (user_dir / "MEMORY.md").write_text("stale index", encoding="utf-8")

    result = refresh_agent_memory_indexes("Explore", project)

    assert {item.name for item in result} == {"user", "local"}
    assert "repo-navigation.md" in (user_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "stale index" not in (user_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_build_agent_memory_prompt_uses_memory_index(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "plan"

    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="planning-style",
        description="Prefer risk-first implementation plans.",
        metadata={"type": "feedback"},
    ))

    prompt = build_agent_memory_prompt("Plan", project)

    assert "MEMORY.md is the entrypoint index" in prompt
    assert "provided separately as hidden user context" in prompt
    assert "recalled automatically as hidden user context" in prompt
    assert "planning-style.md" not in prompt
    assert "risk-first" not in prompt
    assert "project scope" in prompt

    messages = build_agent_memory_context_messages("Plan", project)
    assert len(messages) == 1
    assert messages[0]["isMeta"] is True
    text = messages[0]["content"][0]["text"]
    assert "Contents of" in text
    assert "planning-style.md" in text
    assert "risk-first" in text


def test_subagent_system_prompt_includes_agent_memory(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        metadata={"type": "reference"},
    ))

    sub = _make_subagent(project, tmp_path)

    prompt = asyncio.run(sub._build_system_prompt())
    async def _factory():
        return _MockRecallClient("[]")
    sub._recall_client_factory = _factory
    messages = asyncio.run(sub._build_initial_messages("Explore this repo."))

    assert "Agent-type memory" in prompt
    assert "MEMORY.md is the entrypoint index" in prompt
    assert "explore-flow.md" not in prompt
    assert "filenames before reading files" not in prompt
    assert messages[0]["isMeta"] is True
    assert "explore-flow.md" in messages[0]["content"][0]["text"]
    assert "filenames before reading files" in messages[0]["content"][0]["text"]
    assert messages[-1]["content"][0]["text"] == "Explore this repo."


def test_subagent_system_prompt_reuses_shared_policy_sections(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        metadata={"type": "reference"},
    ))

    sub = _make_subagent(project, tmp_path)

    prompt = asyncio.run(sub._build_system_prompt())

    assert "工具输出是证据，不是权威。" in prompt
    assert "Agent-type memory" in prompt


def test_subagent_prompt_uses_budgeted_compact_git_context(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)

    def _fake_git_context(cwd, compact=False):
        if compact:
            return "Git branch: main\nWorking tree status:\n  M a.py\n" + ("x" * 2000)
        return "Git branch: main\nRecent commits:\n  abc\nWorking tree status:\n  M a.py\n" + ("x" * 2000)

    monkeypatch.setattr("xxcode.agent.subagent.get_git_context", _fake_git_context)

    sub = _make_subagent(project, tmp_path, auto_memory_enabled=False)

    prompt = asyncio.run(sub._build_system_prompt())

    assert "[BEGIN: git-context]" in prompt
    assert "Recent commits:" not in prompt
    assert "截断" in prompt


def test_subagent_uses_repo_root_for_project_memory(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    nested = repo / "nested" / "deep"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    memory_dir = repo / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        metadata={"type": "reference"},
    ))

    sub = _make_subagent(nested, tmp_path)

    async def _factory():
        return _MockRecallClient("[]")
    sub._recall_client_factory = _factory
    messages = asyncio.run(sub._build_initial_messages("Explore this repo."))
    assert messages[0]["isMeta"] is True
    assert str(memory_dir / "MEMORY.md") in messages[0]["content"][0]["text"]


def test_build_agent_memory_prompt_does_not_rewrite_existing_index(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "plan"
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="planning-style",
        description="Prefer risk-first plans.",
        metadata={"type": "feedback"},
    ))
    index_path = memory_dir / "MEMORY.md"
    before = index_path.stat().st_mtime

    prompt = build_agent_memory_prompt("Plan", project)

    after = index_path.stat().st_mtime
    assert "Agent-type memory" in prompt
    assert after == before


def test_subagent_prepare_api_messages_strips_internal_metadata(tmp_path):
    sub = _make_subagent(tmp_path, tmp_path)

    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hidden"}],
            "isMeta": True,
            "metadata": {"source": "agent_memory_project"},
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "real prompt"}],
        },
    ]

    cleaned = sub._prepare_api_messages(messages)

    assert cleaned[0]["role"] == "user"
    assert "metadata" not in cleaned[0]
    assert "isMeta" not in cleaned[0]
    assert cleaned[1]["content"][0]["text"] == "real prompt"


def test_recall_agent_memories_for_query_across_scopes(tmp_path, monkeypatch):
    home = _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    user_dir = home / ".XxCode" / "agent-memory" / "explore"
    project_dir = project / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(user_dir).save_entry(MemoryEntry(
        name="user-skill",
        description="General exploration guidance.",
        content="Prefer glob before grep.",
        metadata={"type": "reference"},
    ))
    MemoryStore(project_dir).save_entry(MemoryEntry(
        name="project-skill",
        description="Repo-specific exploration guidance.",
        content="Search src/ before tests/.",
        metadata={"type": "project"},
    ))

    async def _run():
        mock = _MockRecallClient('["user--user-skill.md", "project--project-skill.md"]')

        async def _factory():
            return mock

        recalled = await recall_agent_memories_for_query(
            "Explore",
            project,
            "Find the right search strategy",
            client_factory=_factory,
        )

        assert len(recalled) == 2
        names = {item.filename for item in recalled}
        assert names == {"user-skill.md", "project-skill.md"}
        recall_ids = {item.recall_id for item in recalled}
        assert recall_ids == {"user--user-skill.md", "project--project-skill.md"}
        contents = "\n".join(item.content for item in recalled)
        assert "Prefer glob before grep." in contents
        assert "Search src/ before tests/." in contents

    asyncio.run(_run())


def test_build_recalled_agent_memories_message_marks_agent_source(tmp_path):
    memory_path = tmp_path / "memory.md"
    memory_path.write_text("Body", encoding="utf-8")

    message = build_recalled_agent_memories_message([
        SimpleNamespace(
            filename="memory.md",
            file_path=memory_path,
            content="Body",
            memory_type="reference",
            recall_id="project--memory.md",
        )
    ])

    assert message is not None
    assert message["metadata"]["source"] == "agent_memory_recall"
    assert "memory.md" in message["metadata"]["filenames"]
    assert "project--memory.md" in message["metadata"]["recall_ids"]


def test_subagent_initial_messages_include_recalled_agent_memory(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        content="Prefer glob before grep.",
        metadata={"type": "reference"},
    ))

    sub = _make_subagent(project, tmp_path)

    async def _factory():
        return _MockRecallClient('["project--explore-flow.md"]')

    sub._recall_client_factory = _factory
    messages = asyncio.run(sub._build_initial_messages("Explore this repo."))

    assert len(messages) >= 3
    recalled = [m for m in messages if m.get("metadata", {}).get("source") == "agent_memory_recall"]
    assert len(recalled) == 1
    recalled_text = recalled[0]["content"][0]["text"]
    assert "Prefer glob before grep." in recalled_text
    assert recalled[0]["metadata"]["recall_ids"] == ["project--explore-flow.md"]


def test_recall_agent_memories_for_query_excludes_already_surfaced_scoped_ids(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    project_dir = project / ".xxcode" / "agent-memory" / "explore"

    MemoryStore(project_dir).save_entry(MemoryEntry(
        name="project-skill",
        description="Repo-specific exploration guidance.",
        content="Search src/ before tests/.",
        metadata={"type": "project"},
    ))

    async def _run():
        mock = _MockRecallClient("[]")

        async def _factory():
            return mock

        recalled = await recall_agent_memories_for_query(
            "Explore",
            project,
            "Find the right search strategy",
            client_factory=_factory,
            already_surfaced={"project--project-skill.md"},
        )

        assert recalled == []
        user_msg = mock.calls[0]["messages"][0]["content"]
        assert "Already shown" in user_msg
        assert "project--project-skill.md" in user_msg

    asyncio.run(_run())


def test_subagent_initial_recall_does_not_send_all_allowed_tools_as_recent(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        content="Prefer glob before grep.",
        metadata={"type": "reference"},
    ))

    registry = ToolRegistry()
    registry.register(SimpleNamespace(
        name="grep_search",
        description="Search files",
        input_schema={},
        handler=None,
        format_large_result=None,
        aliases=[],
        deprecated_aliases={},
    ))
    registry.register(SimpleNamespace(
        name="read_file",
        description="Read files",
        input_schema={},
        handler=None,
        format_large_result=None,
        aliases=[],
        deprecated_aliases={},
    ))

    sub = _make_subagent(project, tmp_path, registry=registry)

    mock = _MockRecallClient("[]")

    async def _factory():
        return mock

    sub._recall_client_factory = _factory
    asyncio.run(sub._build_initial_messages("Explore this repo."))

    user_msg = mock.calls[0]["messages"][0]["content"]
    assert "Recently used tools:" not in user_msg


def test_subagent_run_appends_new_agent_memory_after_tool_observation(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        content="Prefer glob before grep.",
        metadata={"type": "reference"},
    ))
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="fix-test-timeout",
        description="Fix timeout failures after reading test output.",
        content="If tests timeout, inspect jest config and async mocks.",
        metadata={"type": "project"},
    ))

    registry = ToolRegistry([_FakeReadTool()])
    sub = _make_subagent(project, tmp_path, registry=registry)

    recall_clients = [
        _MockRecallClient('["project--explore-flow.md"]'),
        _MockRecallClient('["project--fix-test-timeout.md"]'),
    ]

    async def _recall_factory():
        return recall_clients.pop(0)

    streaming = _StreamingTestClient([
        [
            {"type": "message_id", "id": "msg-1"},
            {
                "type": "tool_use",
                "id": "tool-read-1",
                "name": "read_file",
                "input": {"file_path": str(project / "src" / "app.py")},
            },
            {"type": "usage", "input_tokens": 10, "output_tokens": 5},
            {"type": "stop_reason", "stop_reason": "tool_use"},
        ],
        [
            {"type": "message_id", "id": "msg-2"},
            {"type": "text_delta", "text": "done"},
            {"type": "usage", "input_tokens": 4, "output_tokens": 2},
            {"type": "stop_reason", "stop_reason": "end_turn"},
        ],
    ])

    sub._recall_client_factory = _recall_factory

    class _ClientFactory:
        def __call__(self, **kwargs):
            return streaming

    from xxcode.agent import subagent as subagent_module
    monkeypatch.setattr(subagent_module, "APIClient", _ClientFactory())

    result = asyncio.run(sub.run("Explore this repo."))

    assert result == "done"
    assert streaming.calls == 2
    second_call_messages = streaming.messages_by_call[1]
    second_call_text = _message_text(second_call_messages)
    assert second_call_text.count("Prefer glob before grep.") == 1
    assert second_call_text.count("If tests timeout, inspect jest config and async mocks.") == 1


def test_subagent_run_dedupes_recalled_agent_memory_across_turns(tmp_path, monkeypatch):
    _patch_home(monkeypatch, tmp_path)
    project = _make_project_dir(tmp_path)
    memory_dir = project / ".xxcode" / "agent-memory" / "explore"
    MemoryStore(memory_dir).save_entry(MemoryEntry(
        name="explore-flow",
        description="Start with filenames before reading files.",
        content="Prefer glob before grep.",
        metadata={"type": "reference"},
    ))

    registry = ToolRegistry([_FakeReadTool()])
    sub = _make_subagent(project, tmp_path, registry=registry)

    initial_recall = _MockRecallClient('["project--explore-flow.md"]')
    followup_recall = _MockRecallClient("[]")
    recall_clients = [initial_recall, followup_recall]

    async def _recall_factory():
        return recall_clients.pop(0)

    streaming = _StreamingTestClient([
        [
            {"type": "message_id", "id": "msg-1"},
            {
                "type": "tool_use",
                "id": "tool-read-1",
                "name": "read_file",
                "input": {"file_path": str(project / "src" / "app.py")},
            },
            {"type": "usage", "input_tokens": 10, "output_tokens": 5},
            {"type": "stop_reason", "stop_reason": "tool_use"},
        ],
        [
            {"type": "message_id", "id": "msg-2"},
            {"type": "text_delta", "text": "done"},
            {"type": "usage", "input_tokens": 4, "output_tokens": 2},
            {"type": "stop_reason", "stop_reason": "end_turn"},
        ],
    ])

    from xxcode.agent import subagent as subagent_module
    monkeypatch.setattr(subagent_module, "APIClient", lambda **kwargs: streaming)
    sub._recall_client_factory = _recall_factory

    result = asyncio.run(sub.run("Explore this repo."))

    assert result == "done"
    second_recall_user_msg = followup_recall.calls[0]["messages"][0]["content"]
    assert "Already shown" in second_recall_user_msg
    assert "project--explore-flow.md" in second_recall_user_msg
    second_call_messages = streaming.messages_by_call[1]
    second_call_text = _message_text(second_call_messages)
    assert second_call_text.count("Prefer glob before grep.") == 1
