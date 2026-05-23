"""API client — Anthropic/OpenAI-compatible async client with streaming support."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from .retry import RetryConfig, retry_with_backoff


@dataclass
class APIResponse:
    """Result of a streaming API call."""
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class APIClient:
    """Async API client supporting Anthropic and OpenAI-compatible endpoints."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 16000,
        retry_config: RetryConfig | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.retry_config = retry_config or RetryConfig()

    async def stream_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AsyncGenerator[dict, None]:
        """Stream a chat completion, yielding events for each chunk.

        Yields events like:
            {"type": "text_delta", "text": "..."}
            {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            {"type": "usage", "input_tokens": N, "output_tokens": M}
            {"type": "error", "message": "..."}
        """
        import json

        import httpx

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Build tool schemas for Anthropic API format
        api_tools = []
        for tool in tools:
            api_tools.append({
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            })

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": messages,
            "stream": True,
        }
        if api_tools:
            body["tools"] = api_tools

        def _build_request():
            return httpx.URL(url), headers, body

        async for event in retry_with_backoff(
            self._stream_request,
            _build_request,
            config=self.retry_config,
        ):
            yield event

    async def _stream_request(self, url, headers, body):
        """Execute a single streaming request (no retry logic here)."""
        import json

        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream(
                "POST", url, headers=headers, json=body
            ) as response:
                if response.status_code != 200:
                    error_text = ""
                    async for chunk in response.aiter_text():
                        error_text += chunk
                    yield {"type": "error", "message": f"API error {response.status_code}: {error_text[:500]}"}
                    return

                current_tool_id = None
                current_tool_name = None
                current_tool_input = ""

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")

                    if event_type == "content_block_start":
                        content_block = data.get("content_block", {})
                        if content_block.get("type") == "tool_use":
                            current_tool_id = content_block.get("id", "")
                            current_tool_name = content_block.get("name", "")
                            current_tool_input = ""

                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield {"type": "text_delta", "text": delta.get("text", "")}
                        elif delta.get("type") == "input_json_delta":
                            current_tool_input += delta.get("partial_json", "")

                    elif event_type == "content_block_stop":
                        if current_tool_id:
                            try:
                                parsed_input = json.loads(current_tool_input) if current_tool_input.strip() else {}
                            except json.JSONDecodeError:
                                parsed_input = {}
                            yield {
                                "type": "tool_use",
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": parsed_input,
                            }
                            current_tool_id = None
                            current_tool_name = None
                            current_tool_input = ""

                    elif event_type == "message_delta":
                        usage = data.get("usage", {})
                        yield {
                            "type": "usage",
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                        }
                        yield {
                            "type": "stop_reason",
                            "stop_reason": data.get("delta", {}).get("stop_reason", "end_turn"),
                        }
