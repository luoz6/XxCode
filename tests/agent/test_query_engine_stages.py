from xxcode.agent.query_engine import QueryEngine
from xxcode.agent.state import AgentState
from xxcode.config import Config
from tests.conftest import make_test_config


def test_query_engine_commit_user_turn_updates_state(tmp_path):
    config = Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = QueryEngine(config)
    state = AgentState()

    engine._commit_user_turn(state, "hello")

    assert state.last_query == "hello"
    assert state.user_turn_count == 1
    assert state.memory_writes_since_extraction is False
    assert state.messages[-1]["role"] == "user"
    assert state.messages[-1]["content"][0]["text"] == "hello"


def test_query_engine_builds_sectioned_system_prompt(tmp_path):
    config = make_test_config(
        tmp_path,
        auto_memory_enabled=False,
        mcp_enabled=False,
        skills_enabled=False,
    )
    engine = QueryEngine(config)
    state = AgentState()

    engine._build_or_refresh_system_prompt(state, tmp_path)

    assert "## 指令优先级" in state.system_prompt
    assert "## 信任与外部上下文" in state.system_prompt
    assert "## 环境信息" in state.system_prompt
