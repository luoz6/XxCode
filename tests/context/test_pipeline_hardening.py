import pytest

from xxcode.agent.state import AgentState
from xxcode.config import Config
from xxcode.context.pipeline import ContextPipeline


def _large_messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "x" * 900_000}]},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    ]


@pytest.mark.asyncio
async def test_autocompact_failures_persist_on_agent_state(tmp_path, monkeypatch):
    config = Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
    )
    state = AgentState(system_prompt="system")

    async def _fail_autocompact(self, messages, system_prompt):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(ContextPipeline, "_autocompact", _fail_autocompact)
    monkeypatch.setattr(
        "xxcode.context.pipeline.apply_collapse_if_needed",
        lambda messages, current_tokens, collapse_threshold_tokens, existing_regions=None: (False, []),
    )
    monkeypatch.setattr(
        "xxcode.context.pipeline.should_autocompact",
        lambda **kwargs: True,
    )

    for _ in range(2):
        pipeline = ContextPipeline(config)
        await pipeline.compress(
            _large_messages(),
            current_tokens=190_000,
            system_prompt="system",
            state=state,
        )

    assert state.consecutive_autocompact_failures == 2
