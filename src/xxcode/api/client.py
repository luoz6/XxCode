"""LLM API client — Adapter pattern supporting Anthropic, DeepSeek, and OpenAI backends.

Architecture:
  LLMClient (ABC)
  ├── AnthropicClient  — Anthropic Messages API (native format)
  └── DeepSeekClient   — OpenAI-compatible format (DeepSeek, GPT, etc.)

Factory: create_llm_client() routes on api_model prefix.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..context.micro import CacheEdit
from .retry import RetryConfig, retry_with_backoff

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Shared types
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class APIResponse:
    """Result of a streaming API call."""
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMRequestOptions:
    """Provider-specific options for one streaming request."""

    anthropic_cache_edits: list[CacheEdit] | None = None


# ═══════════════════════════════════════════════════════════════════════
# Canonical pricing tables (USD per 1M tokens)
#
# These values are divided by 1000 by _calculate_cost() in loop.py to
# derive per-1K prices before multiplying by token counts.
# Format: {"input": usd_per_1M, "output": usd_per_1M}
#
# get_pricing() uses longest-prefix-first matching, so specific model
# keys (e.g. "deepseek-v4-flash") take priority over shorter prefixes
# (e.g. "deepseek-chat") when both could match a model string.
# ═══════════════════════════════════════════════════════════════════════

PRICING = {
    # Anthropic models
    "claude-opus-4":       {"input": 15.00, "output": 75.00},
    "claude-sonnet-4":     {"input":  3.00, "output": 15.00},
    "claude-haiku-4":      {"input":  0.80, "output":  4.00},
    # DeepSeek models
    "deepseek-v4-pro":     {"input":  0.55, "output": 2.19},
    "deepseek-v4-flash":   {"input":  0.14, "output": 0.28},
    "deepseek-reasoner":   {"input":  0.55, "output": 2.19},
    "deepseek-chat":       {"input":  0.27, "output": 1.10},
    # OpenAI GPT models
    "gpt-5":               {"input":  1.25, "output": 10.00},
    "gpt-5-mini":          {"input":  0.35, "output":  2.00},
    "gpt-5-nano":          {"input":  0.10, "output":  0.50},
    "gpt-4.1":             {"input":  2.00, "output":  8.00},
    "gpt-4.1-mini":        {"input":  0.40, "output":  2.00},
    "gpt-4.1-nano":        {"input":  0.10, "output":  0.80},
    "gpt-4o":              {"input":  2.50, "output": 10.00},
    "gpt-4o-mini":         {"input":  0.15, "output":  0.60},
    "o4-mini":             {"input":  1.10, "output":  4.40},
    "o3":                  {"input": 10.00, "output": 40.00},
    "o3-mini":             {"input":  1.10, "output":  4.40},
    "o1":                  {"input": 15.00, "output": 60.00},
}

# Default pricing for unknown models (conservative — assumes Claude Sonnet)
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


def get_pricing(model: str) -> dict[str, float]:
    """Look up (input, output) pricing per 1M tokens for a model.

    Returns {"input": price_per_1M, "output": price_per_1M}.
    Falls back to exact-prefix match, then per-family prefix, then default.
    """
    model_lower = model.lower().strip()
    if model_lower in PRICING:
        return dict(PRICING[model_lower])

    # Family prefix match (e.g. "claude-sonnet-4-6" → "claude-sonnet-4")
    for prefix in sorted(PRICING, key=lambda p: -len(p)):
        if model_lower.startswith(prefix):
            return dict(PRICING[prefix])

    return dict(_DEFAULT_PRICING)


def _is_deepseek_model(model: str) -> bool:
    return model.lower().strip().startswith("deepseek")


def _is_openai_model(model: str) -> bool:
    """Check if the model name indicates an OpenAI GPT / o-series model."""
    model_lower = model.lower().strip()
    return model_lower.startswith(("gpt-", "o1", "o3", "o4"))


def _is_openai_compatible_model(model: str) -> bool:
    """Models that use the OpenAI-compatible chat completions endpoint."""
    return _is_deepseek_model(model) or _is_openai_model(model)


# ═══════════════════════════════════════════════════════════════════════
# Tool schema converter: internal format ↔ OpenAI format
# ═══════════════════════════════════════════════════════════════════════

def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert from Anthropic tool schema to OpenAI function schema.

    Anthropic: {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI:    {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", tool.get("parameters", {})),
            },
        })
    return openai_tools


# ═══════════════════════════════════════════════════════════════════════
# Normalized stream event format shared by all backends
# ═══════════════════════════════════════════════════════════════════════

# All clients MUST yield events with these types:
#   text_delta        {"type": "text_delta", "text": "..."}
#   tool_use          {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
#   thinking          {"type": "thinking", "text": "...", "signature": "..."}
#   thinking_delta    {"type": "thinking_delta", "text": "..."}
#   signature_delta   {"type": "signature_delta", "signature": "..."}
#   redacted_thinking {"type": "redacted_thinking", "data": "..."}
#   message_id        {"type": "message_id", "id": "..."}
#   usage             {"type": "usage", "input_tokens": N, "output_tokens": M}
#   stop_reason       {"type": "stop_reason", "stop_reason": "..."}
#   error             {"type": "error", "message": "..."}


# ═══════════════════════════════════════════════════════════════════════
# Abstract base
# ═══════════════════════════════════════════════════════════════════════

class LLMClient(ABC):
    """Abstract base for LLM API clients.

    Subclasses implement stream_chat() and complete() for a specific
    provider (Anthropic, DeepSeek, etc.).  All clients yield the same
    normalized event format so the rest of the system is provider-agnostic.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 16000,
        thinking_budget_tokens: int | None = None,
        retry_config: RetryConfig | None = None,
    ):
        if not api_key:
            raise ValueError("API key must be configured explicitly.")
        if not base_url:
            raise ValueError("API base URL must be configured explicitly.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.thinking_budget_tokens = thinking_budget_tokens
        self.retry_config = retry_config or RetryConfig()

    @abstractmethod
    async def stream_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        *,
        options: LLMRequestOptions | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream a chat completion, yielding normalized events."""
        ...

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        """Non-streaming completion for lightweight side queries."""
        ...

    async def _retry_request(
        self,
        make_request,
        *,
        timeout: float = 30.0,
    ):
        """Execute a non-streaming HTTP request with retry logic.

        Args:
            make_request: Async callable that returns an httpx.Response.
            timeout: Request timeout in seconds.

        Returns:
            The parsed JSON response body on success.

        Raises:
            RuntimeError: On non-retryable errors or exhausted retries.
        """
        import asyncio as _asyncio
        import random as _random

        cfg = self.retry_config

        for attempt in range(cfg.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=10.0)
                ) as http:
                    response = await make_request(http)

                    if response.status_code == 200:
                        try:
                            return response.json()
                        except json.JSONDecodeError as e:
                            raise RuntimeError(
                                f"Invalid JSON in 200 response: {response.text[:200]}"
                            ) from e

                    error_text = response.text[:500]

                # Determine if this error is retryable
                is_retryable = False
                if response.status_code in cfg.retryable_statuses:
                    is_retryable = True
                elif response.status_code in cfg.conditional_retry_statuses:
                    is_retryable = not any(
                        kw in error_text for kw in cfg.non_retryable_keywords
                    )

                if is_retryable and attempt < cfg.max_retries:
                    delay = cfg.base_delay * (cfg.backoff_factor ** attempt)
                    delay = min(delay, cfg.max_delay)
                    if cfg.jitter:
                        delay *= 0.5 + _random.random()
                    await _asyncio.sleep(delay)
                    continue

                raise RuntimeError(
                    f"API error {response.status_code}: {error_text}"
                )

            except RuntimeError:
                raise
            except Exception as e:
                if attempt < cfg.max_retries:
                    delay = cfg.base_delay * (cfg.backoff_factor ** attempt)
                    delay = min(delay, cfg.max_delay)
                    if cfg.jitter:
                        delay *= 0.5 + _random.random()
                    await _asyncio.sleep(delay)
                else:
                    raise RuntimeError(
                        f"Request failed after {cfg.max_retries + 1} attempts: {e}"
                    ) from e


# ═══════════════════════════════════════════════════════════════════════
# Anthropic client
# ═══════════════════════════════════════════════════════════════════════

class AnthropicClient(LLMClient):
    """Anthropic Messages API client (native format)."""

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        system_block = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_block,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if self.thinking_budget_tokens:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }

        async def _do_request(http):
            return await http.post(url, headers=headers, json=body)

        data = await self._retry_request(_do_request)
        content_blocks = data.get("content", [])
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        ]
        return "".join(text_parts)

    async def stream_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        *,
        options: LLMRequestOptions | None = None,
    ) -> AsyncGenerator[dict, None]:
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Build Anthropic-format tool schemas.
        # Last tool carries cache_control for prompt caching.
        api_tools = []
        for tool in tools:
            entry = {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", tool.get("parameters", {})),
            }
            api_tools.append(entry)
        if api_tools:
            api_tools[-1] = {**api_tools[-1], "cache_control": {"type": "ephemeral"}}

        system_block = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_block,
            "messages": messages,
            "stream": True,
        }
        if api_tools:
            body["tools"] = api_tools
        if self.thinking_budget_tokens:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }
        if options and options.anthropic_cache_edits:
            logger.debug(
                "Anthropic cache edits requested but request schema is not enabled."
            )

        def _build_request():
            return httpx.URL(url), headers, body

        async for event in retry_with_backoff(
            self._stream_anthropic,
            _build_request,
            config=self.retry_config,
        ):
            yield event

    async def _stream_anthropic(self, url, headers, body):
        """Execute a single Anthropic streaming request (no retry)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != 200:
                    error_text = ""
                    async for chunk in response.aiter_text():
                        error_text += chunk
                    is_retryable = False
                    if response.status_code in (429,):
                        is_retryable = True
                    elif response.status_code in (503, 529):
                        non_retryable = (
                            "model_not_found", "invalid_request_error",
                            "invalid_api_key", "authentication",
                            "authorization", "not_found",
                        )
                        is_retryable = not any(kw in error_text for kw in non_retryable)

                    if is_retryable:
                        from .retry import _RetryableError
                        raise _RetryableError(f"API error {response.status_code}: {error_text[:200]}")
                    else:
                        yield {"type": "error", "message": f"API error {response.status_code}: {error_text[:500]}"}
                        return

                current_tool_id = None
                current_tool_name = None
                current_tool_input = ""
                saved_input_tokens = 0

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:]

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")

                    if event_type == "error":
                        yield {
                            "type": "error",
                            "message": data.get("error", {}).get("message", "Unknown API error"),
                        }
                        return

                    if event_type == "message_start":
                        message = data.get("message", {})
                        if message.get("id"):
                            yield {"type": "message_id", "id": message["id"]}
                        saved_input_tokens = message.get("usage", {}).get("input_tokens", 0)

                    elif event_type == "content_block_start":
                        content_block = data.get("content_block", {})
                        block_type = content_block.get("type", "")
                        if block_type == "tool_use":
                            current_tool_id = content_block.get("id", "")
                            current_tool_name = content_block.get("name", "")
                            current_tool_input = ""
                        elif block_type == "thinking":
                            yield {
                                "type": "thinking",
                                "text": content_block.get("thinking", ""),
                                "signature": content_block.get("signature", ""),
                            }
                        elif block_type == "redacted_thinking":
                            yield {
                                "type": "redacted_thinking",
                                "data": content_block.get("data", ""),
                            }

                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        delta_type = delta.get("type", "")
                        if delta_type == "text_delta":
                            yield {"type": "text_delta", "text": delta.get("text", "")}
                        elif delta_type == "input_json_delta":
                            current_tool_input += delta.get("partial_json", "")
                        elif delta_type == "thinking_delta":
                            yield {"type": "thinking_delta", "text": delta.get("thinking", "")}
                        elif delta_type == "signature_delta":
                            yield {"type": "signature_delta", "signature": delta.get("signature", "")}

                    elif event_type == "content_block_stop":
                        if current_tool_id:
                            try:
                                parsed_input = json.loads(current_tool_input) if current_tool_input.strip() else {}
                            except json.JSONDecodeError:
                                yield {
                                    "type": "error",
                                    "message": f"Failed to parse tool_use input for {current_tool_name}: malformed JSON",
                                }
                                current_tool_id = None
                                current_tool_name = None
                                current_tool_input = ""
                                continue
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
                            "input_tokens": saved_input_tokens,
                            "output_tokens": usage.get("output_tokens", 0),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                        }
                        yield {
                            "type": "stop_reason",
                            "stop_reason": data.get("delta", {}).get("stop_reason", "end_turn"),
                        }


# ═══════════════════════════════════════════════════════════════════════
# DeepSeek client (OpenAI-compatible format)
# ═══════════════════════════════════════════════════════════════════════

class DeepSeekClient(LLMClient):
    """DeepSeek API client using OpenAI-compatible chat completions format.

    Key differences from Anthropic:
      - Endpoint: /chat/completions (not /v1/messages)
      - Auth: Bearer token (not x-api-key)
      - System prompt: in messages[0] as role="system"
      - Tools: OpenAI function-calling format with type:"function" wrapper
      - No anthropic-version header, no cache_control, no native thinking
    """

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _chat_completions_url(self) -> str:
        """Return DeepSeek's OpenAI-compatible chat completions endpoint."""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        return f"{base_url}/chat/completions"

    def _build_messages(self, system_prompt: str, messages: list[dict]) -> list[dict]:
        """Insert system prompt as the first message and convert to OpenAI format."""
        converted = self._convert_anthropic_messages_to_openai(messages)
        result: list[dict] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(converted)
        return result

    @staticmethod
    def _is_system_hint_text(text: str) -> bool:
        return "<system_hint>" in text

    @staticmethod
    def _tool_message_from_result(result: dict[str, Any]) -> dict[str, Any]:
        content = result["content"]
        if result["is_error"]:
            content = f"[ERROR] {content}"
        return {
            "role": "tool",
            "tool_call_id": result["tool_use_id"],
            "content": content,
        }

    @staticmethod
    def _fallback_tool_message(tool_call_id: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "Tool execution was interrupted or lost before the provider request was retried.",
        }

    @staticmethod
    def _split_anthropic_blocks(
        content: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif block_type == "tool_result":
                tool_results.append({
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": DeepSeekClient._extract_tool_result_text(block),
                    "is_error": block.get("is_error", False),
                })
        return text_parts, tool_calls, tool_results

    @staticmethod
    def _consume_tool_result_carrier(
        message: dict[str, Any],
        expected_ids: set[str],
    ) -> tuple[list[dict[str, Any]], str, set[str]] | None:
        """Consume one compatible tool-result carrier or reject it whole.

        This helper is intentionally conservative: if a carrier message mixes
        tool results with ordinary user-authored text, or mixes tool_result
        ids from different assistant turns, return None so the caller can stop
        the boundary instead of partially consuming the message.
        """
        if message.get("role") != "user":
            return None

        content = message.get("content", [])
        if not isinstance(content, list):
            return None

        text_parts: list[str] = []
        matched_ids: set[str] = set()
        tool_messages: list[dict[str, Any]] = []
        saw_tool_result = False

        for block in content:
            if not isinstance(block, dict):
                return None

            block_type = block.get("type", "")
            if block_type == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id not in expected_ids:
                    return None
                saw_tool_result = True
                matched_ids.add(tool_use_id)
                tool_messages.append(
                    DeepSeekClient._tool_message_from_result({
                        "tool_use_id": tool_use_id,
                        "content": DeepSeekClient._extract_tool_result_text(block),
                        "is_error": block.get("is_error", False),
                    })
                )
                continue

            if block_type == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    return None
                if DeepSeekClient._is_system_hint_text(text):
                    text_parts.append(text)
                    continue
                return None

            return None

        if not saw_tool_result:
            return None

        return tool_messages, "\n".join(part for part in text_parts if part), matched_ids

    @staticmethod
    def _convert_anthropic_messages_to_openai(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format content blocks to OpenAI-compatible messages."""
        converted: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                converted.append({"role": role, "content": content})
                i += 1
                continue

            if not isinstance(content, list):
                converted.append({"role": role, "content": str(content)})
                i += 1
                continue

            text_parts, tool_calls, tool_results = DeepSeekClient._split_anthropic_blocks(content)

            if role == "assistant" and tool_calls:
                assistant_entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else "",
                    "tool_calls": tool_calls,
                }
                converted.append(assistant_entry)

                expected_ids = {
                    call.get("id", "")
                    for call in tool_calls
                    if call.get("id", "")
                }
                i += 1

                while i < len(messages):
                    consumed = DeepSeekClient._consume_tool_result_carrier(
                        messages[i],
                        expected_ids,
                    )
                    if consumed is None:
                        break

                    tool_messages, trailing_text, matched_ids = consumed
                    converted.extend(tool_messages)
                    if trailing_text:
                        converted.append({"role": "user", "content": trailing_text})
                    expected_ids -= matched_ids
                    i += 1

                for missing_id in sorted(expected_ids):
                    converted.append(DeepSeekClient._fallback_tool_message(missing_id))
                continue

            if role == "user" and tool_results:
                if text_parts:
                    converted.append({"role": "user", "content": "\n".join(text_parts)})
                i += 1
                continue

            if text_parts:
                converted.append({"role": role, "content": "\n".join(text_parts)})
                i += 1
                continue

            if role == "assistant":
                converted.append({"role": role, "content": ""})
                i += 1
                continue

            if role == "user":
                converted.append({"role": role, "content": ""})
                i += 1
                continue

            i += 1
            continue

        return converted

    @staticmethod
    def _extract_tool_result_text(block: dict) -> str:
        """Extract text content from an Anthropic tool_result block."""
        result_content = block.get("content", "")
        if isinstance(result_content, str):
            return result_content
        if isinstance(result_content, list):
            parts = []
            for item in result_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(parts)
        if result_content is None:
            return ""
        return str(result_content)

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        url = self._chat_completions_url()
        headers = self._build_headers()

        openai_messages = self._build_messages(system_prompt, messages)

        body: dict = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = anthropic_tools_to_openai(tools)

        async def _do_request(http):
            return await http.post(url, headers=headers, json=body)

        data = await self._retry_request(_do_request)
        choices = data.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("content", "") or ""

    async def stream_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        *,
        options: LLMRequestOptions | None = None,
    ) -> AsyncGenerator[dict, None]:
        url = self._chat_completions_url()
        headers = self._build_headers()

        openai_messages = self._build_messages(system_prompt, messages)
        openai_tools = anthropic_tools_to_openai(tools) if tools else []

        body = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            body["tools"] = openai_tools

        def _build_request():
            return httpx.URL(url), headers, body

        async for event in retry_with_backoff(
            self._stream_deepseek,
            _build_request,
            config=self.retry_config,
        ):
            yield event

    async def _stream_deepseek(self, url, headers, body):
        """Execute a single DeepSeek streaming request (no retry)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != 200:
                    error_text = ""
                    async for chunk in response.aiter_text():
                        error_text += chunk
                    is_retryable = False
                    if response.status_code in (429,):
                        is_retryable = True
                    elif response.status_code in (500, 503, 529):
                        non_retryable = (
                            "model_not_found", "invalid_request_error",
                            "invalid_api_key", "authentication",
                            "authorization", "not_found",
                        )
                        is_retryable = not any(kw in error_text for kw in non_retryable)
                    if is_retryable:
                        from .retry import _RetryableError
                        raise _RetryableError(f"DeepSeek API error {response.status_code}: {error_text[:200]}")
                    else:
                        yield {"type": "error", "message": f"DeepSeek API error {response.status_code}: {error_text[:500]}"}
                        return

                # Per-tool-call accumulation state
                tool_call_buffers: dict[int, dict] = {}
                finish_reason: str | None = None
                data: dict = {}

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason") or finish_reason

                    # ── Text content ──
                    text_content = delta.get("content")
                    if text_content:
                        yield {"type": "text_delta", "text": text_content}

                    # ── Tool calls ──
                    tc_list = delta.get("tool_calls", [])
                    for tc in tc_list:
                        idx = tc.get("index", 0)
                        if idx not in tool_call_buffers:
                            tool_call_buffers[idx] = {
                                "id": tc.get("id", ""),
                                "name": "",
                                "arguments": "",
                            }

                        buf = tool_call_buffers[idx]
                        if "id" in tc and tc["id"]:
                            buf["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if "name" in fn and fn["name"]:
                            buf["name"] = fn["name"]
                        if "arguments" in fn:
                            buf["arguments"] += fn["arguments"]

                # ── Post-stream processing ──
                # Emit message_id from the response (DeepSeek provides 'id' at top level)
                if data.get("id"):
                    yield {"type": "message_id", "id": data["id"]}

                if finish_reason == "tool_calls":
                    for idx in sorted(tool_call_buffers):
                        buf = tool_call_buffers[idx]
                        if buf["name"]:
                            try:
                                parsed = json.loads(buf["arguments"]) if buf["arguments"].strip() else {}
                            except json.JSONDecodeError:
                                parsed = {}
                            yield {
                                "type": "tool_use",
                                "id": buf["id"],
                                "name": buf["name"],
                                "input": parsed,
                            }
                        elif buf["arguments"]:
                            yield {
                                "type": "error",
                                "message": f"Tool call at index {idx} is missing a function name; arguments were: {buf['arguments'][:200]}",
                            }

                # Usage data from the last chunk
                usage = data.get("usage", {})
                if usage:
                    yield {
                        "type": "usage",
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                    }

                stop_reason_map = {
                    "stop": "end_turn",
                    "tool_calls": "tool_use",
                    "length": "max_tokens",
                    "content_filter": "refusal",
                }
                mapped = stop_reason_map.get(finish_reason or "", finish_reason or "end_turn")
                yield {"type": "stop_reason", "stop_reason": mapped}


# ═══════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════

def create_llm_client(
    api_key: str,
    base_url: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 16000,
    thinking_budget_tokens: int | None = None,
    retry_config: RetryConfig | None = None,
) -> LLMClient:
    """Factory: route to the correct LLM client based on model name.

    deepseek/gpt/o-series models → DeepSeekClient (OpenAI-compatible format).
    Everything else → AnthropicClient (Anthropic Messages API).
    """
    if _is_openai_compatible_model(model):
        return DeepSeekClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            thinking_budget_tokens=thinking_budget_tokens,
            retry_config=retry_config,
        )
    return AnthropicClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
        retry_config=retry_config,
    )


# ═══════════════════════════════════════════════════════════════════════
# Backward-compatibility alias
# ═══════════════════════════════════════════════════════════════════════

# APIClient is kept as a backward-compatible alias that delegates to
# the factory.  Code that directly constructs APIClient(...) will still
# work and get the correct backend.
class APIClient:
    """Backward-compatible APIClient that delegates to the adapter factory.

    New code should use create_llm_client() directly.  This class exists
    so that existing call sites (subagent.py, extraction.py, etc.) don't
    break while they're migrated.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 16000,
        thinking_budget_tokens: int | None = None,
        retry_config: RetryConfig | None = None,
    ):
        self._delegate = create_llm_client(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            thinking_budget_tokens=thinking_budget_tokens,
            retry_config=retry_config,
        )

    @property
    def api_key(self):
        return self._delegate.api_key

    @property
    def base_url(self):
        return self._delegate.base_url

    @property
    def model(self):
        return self._delegate.model

    @property
    def max_tokens(self):
        return self._delegate.max_tokens

    @property
    def thinking_budget_tokens(self):
        return self._delegate.thinking_budget_tokens

    @property
    def retry_config(self):
        return self._delegate.retry_config

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        max_tokens: int = 1024,
        tools: list[dict] | None = None,
    ) -> str:
        return await self._delegate.complete(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
        )

    async def stream_chat(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        *,
        options: LLMRequestOptions | None = None,
    ) -> AsyncGenerator[dict, None]:
        async for event in self._delegate.stream_chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            options=options,
        ):
            yield event
