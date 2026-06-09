import inspect

from xxcode.api.client import APIClient, AnthropicClient, DeepSeekClient, LLMRequestOptions
from xxcode.config import Config
from xxcode.context.micro import CacheEdit


def test_config_exposes_cache_ttl_and_anthropic_cache_edit_flag(monkeypatch):
    monkeypatch.setenv("XXCODE_PROMPT_CACHE_TTL_SECONDS", "123.5")
    monkeypatch.setenv("XXCODE_ANTHROPIC_CACHE_EDITS", "true")

    config = Config()

    assert config.prompt_cache_ttl_seconds == 123.5
    assert config.anthropic_cache_edits_enabled is True


def test_request_options_carries_internal_cache_edit_type():
    edit = CacheEdit(tool_use_id="tool-1")
    options = LLMRequestOptions(anthropic_cache_edits=[edit])

    assert options.anthropic_cache_edits == [edit]


def test_stream_chat_signatures_accept_keyword_only_options():
    for cls in (AnthropicClient, DeepSeekClient, APIClient):
        signature = inspect.signature(cls.stream_chat)
        assert "options" in signature.parameters
        assert signature.parameters["options"].kind is inspect.Parameter.KEYWORD_ONLY


def test_deepseek_ignores_anthropic_options_when_building_messages():
    client = DeepSeekClient(api_key="key", base_url="https://example.test", model="deepseek-chat")
    options = LLMRequestOptions(anthropic_cache_edits=[CacheEdit(tool_use_id="tool-1")])

    assert hasattr(client, "_build_messages")
    built = client._build_messages(
        "system",
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    )

    assert built == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert options.anthropic_cache_edits[0].tool_use_id == "tool-1"
