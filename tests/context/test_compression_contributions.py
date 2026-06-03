import pytest

import xxcode.context.pipeline as pipeline_module

from xxcode.config import Config
from xxcode.context.pipeline import ContextPipeline


def _make_pipeline(tmp_path):
    config = Config(
        api_key="test-key",
        api_base_url="https://example.test",
        api_model="test-model",
        cwd=tmp_path,
        session_dir=tmp_path / "sessions",
    )
    return ContextPipeline(config)


def _tool_result_message(text: str, tool_use_id: str = "tool-1") -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
            }
        ],
    }


@pytest.mark.asyncio
async def test_l1_reports_token_and_character_contributions(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    noisy = (
        "Collecting demo-package\n"
        "Downloading demo-package\n"
        "Requirement already satisfied: demo-package\n"
        "Successfully installed demo-package\n\n"
        + ("Collecting demo-package\nDownloading demo-package\n" * 120)
    )

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    compressed, stats = await pipeline.compress(
        [_tool_result_message(noisy)],
        current_tokens=None,
        context_limit=200,
        threshold=0.5,
    )

    assert compressed != [_tool_result_message(noisy)]
    assert stats.snip_removed > 0
    assert stats.snip_tokens_freed > 0
    assert stats.micro_tokens_freed == 0
    assert stats.collapse_tokens_freed == 0
    assert stats.auto_tokens_freed == 0
    assert stats.tokens_before - stats.tokens_after == stats.snip_tokens_freed


@pytest.mark.asyncio
async def test_l1_no_noise_keeps_zero_contribution(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path)
    clean = "\n".join(["plain output"] * 400)

    monkeypatch.setattr(pipeline_module, "should_autocompact", lambda **kwargs: False)

    _compressed, stats = await pipeline.compress(
        [_tool_result_message(clean)],
        current_tokens=None,
        context_limit=200,
        threshold=0.2,
    )

    assert stats.snip_removed == 0
    assert stats.snip_tokens_freed == 0
    assert stats.level_reached >= 1
