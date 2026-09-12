"""Text-only LLM completion service.

Reuses the existing LLMConfig from AgentConfig to generate
text completions. Supports OpenAI, Ollama, BYOM (custom
OpenAI-compatible endpoints), Anthropic, OpenRouter and Bedrock.

This is a separate dispatch from the voice pipeline: it speaks raw HTTP rather
than building Pipecat services, so every provider needs an implementation in
both places. See ADR-0016.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.config.settings import get_settings
from turncall.domain.models import AWSConfig, LLMConfig

# Runs one tool and returns its result as a string. Owns its own errors —
# a failure comes back as text for the model to react to, never as an
# exception, so one bad webhook can't sink the whole reply.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

# Text has no turn-taking pressure, so nothing naturally ends a tool loop.
_MAX_TOOL_ROUNDS = 5


@dataclass(frozen=True)
class CompletionResult:
    """Result of a text completion."""

    text: str
    total_tokens: int


def _resolve_api_key(config: LLMConfig) -> str:
    """Resolve the API key from config or environment."""
    if config.api_key:
        return config.api_key
    settings = get_settings()
    if config.provider in ("openai", "custom_openai"):
        return settings.openai.api_key
    if config.provider == "anthropic":
        return settings.anthropic.api_key
    if config.provider == "openrouter":
        return settings.openrouter.api_key
    return ""


def _resolve_base_url(config: LLMConfig) -> str:
    """Resolve the base URL for the LLM provider."""
    if config.base_url:
        return config.base_url.rstrip("/")
    if config.provider == "ollama":
        return "http://localhost:11434/v1"
    if config.provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    return "https://api.openai.com/v1"


def _anthropic_text(blocks: list[dict[str, Any]]) -> str:
    """Join the text blocks of a Messages response.

    Not `content[0]["text"]`: a reply that leads with a thinking or tool_use
    block would raise KeyError, and tool use makes that the normal case.
    """
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


async def _complete_text_anthropic(
    config: LLMConfig,
    messages: list[dict[str, Any]],
    api_key: str,
    tools: list[dict[str, Any]] | None = None,
    execute_tool: ToolExecutor | None = None,
) -> CompletionResult:
    """Generate a text completion via the Anthropic Messages API.

    Tools use `input_schema` rather than `parameters`, and results go back as
    tool_result content blocks on a user turn rather than a `tool` role.
    """
    # Separate system messages from conversation messages
    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(msg["content"])
        else:
            conversation.append(msg)

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": config.model,
        "messages": conversation,
        "max_tokens": config.max_tokens,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if tools:
        body["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    client = get_http_client()
    total_tokens = 0

    async def _round() -> list[dict[str, Any]]:
        nonlocal total_tokens
        response = await client.post(url, json=body, headers=headers, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return data.get("content", [])

    for _ in range(_MAX_TOOL_ROUNDS):
        blocks = await _round()
        calls = (
            [b for b in blocks if b.get("type") == "tool_use"] if execute_tool else []
        )
        if not calls:
            return CompletionResult(
                text=_anthropic_text(blocks), total_tokens=total_tokens
            )

        # The whole content array goes back, thinking and text blocks included.
        body["messages"].append({"role": "assistant", "content": blocks})
        results = await _run_calls(
            execute_tool,
            [(c.get("name", ""), c.get("input") or {}) for c in calls],
        )
        body["messages"].append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id", ""),
                        "content": result,
                    }
                    for call, result in zip(calls, results, strict=True)
                ],
            }
        )

    logger.warning(
        "chat_tool_rounds_exhausted", model=config.model, rounds=_MAX_TOOL_ROUNDS
    )
    body.pop("tools", None)
    return CompletionResult(
        text=_anthropic_text(await _round()), total_tokens=total_tokens
    )


async def _complete_text_bedrock(
    config: LLMConfig,
    aws: AWSConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    execute_tool: ToolExecutor | None = None,
) -> CompletionResult:
    """Generate a text completion via the Bedrock converse API (ADR-0016).

    Converse wraps each tool in a `toolSpec` and each result in a `toolResult`
    content block; the schema goes under `inputSchema.json`. Shapes taken from
    botocore's own service model rather than prose docs.
    """
    import boto3

    from turncall.services.aws_credentials import resolve_aws_credentials

    credentials = resolve_aws_credentials(aws)

    # converse() takes system prompts out of band, like the Anthropic path.
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    conversation: list[dict[str, Any]] = [
        {"role": m["role"], "content": [{"text": m["content"]}]}
        for m in messages
        if m.get("role") != "system"
    ]

    kwargs: dict[str, Any] = {
        "modelId": config.model,
        "messages": conversation,
        "inferenceConfig": {
            "maxTokens": config.max_tokens,
            "temperature": config.temperature,
        },
    }
    if system_parts:
        kwargs["system"] = [{"text": "\n\n".join(system_parts)}]
    if config.extra:
        kwargs["additionalModelRequestFields"] = config.extra
    if tools:
        kwargs["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "inputSchema": {
                            "json": t.get("parameters")
                            or {"type": "object", "properties": {}}
                        },
                    }
                }
                for t in tools
            ]
        }

    # One client for the whole exchange rather than one per round.
    client = boto3.client(
        "bedrock-runtime",
        region_name=credentials.region,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
        aws_session_token=credentials.session_token,
    )
    total_tokens = 0

    async def _round() -> dict[str, Any]:
        nonlocal total_tokens
        try:
            # boto3 is synchronous; keep it off the event loop.
            response = await asyncio.to_thread(lambda: client.converse(**kwargs))
        except Exception as exc:
            # Bedrock availability is region-specific and the raw error names
            # neither side of the pair that failed. ADR-0016.
            msg = (
                f"Bedrock converse failed for model {config.model!r} in region "
                f"{credentials.region!r}: {exc}"
            )
            raise RuntimeError(msg) from exc

        total_tokens += response.get("usage", {}).get("totalTokens", 0)
        return response["output"]["message"]

    def _text(message: dict[str, Any]) -> str:
        return "".join(b["text"] for b in message.get("content", []) if "text" in b)

    for _ in range(_MAX_TOOL_ROUNDS):
        message = await _round()
        blocks = message.get("content", [])
        calls = [b["toolUse"] for b in blocks if "toolUse" in b] if execute_tool else []
        if not calls:
            return CompletionResult(text=_text(message), total_tokens=total_tokens)

        kwargs["messages"].append(message)
        results = await _run_calls(
            execute_tool,
            [(c.get("name", ""), c.get("input") or {}) for c in calls],
        )
        kwargs["messages"].append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": call.get("toolUseId", ""),
                            "content": [{"text": result}],
                        }
                    }
                    for call, result in zip(calls, results, strict=True)
                ],
            }
        )

    logger.warning(
        "chat_tool_rounds_exhausted", model=config.model, rounds=_MAX_TOOL_ROUNDS
    )
    kwargs.pop("toolConfig", None)
    return CompletionResult(text=_text(await _round()), total_tokens=total_tokens)


async def _run_calls(
    execute_tool: ToolExecutor,
    calls: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    """Run one round's tool calls concurrently, results in request order.

    Models routinely ask for several at once. Awaiting them in turn makes the
    reply wait for the sum of their latencies instead of the slowest, which on
    SMS competes with the provider's delivery timeout.
    """
    if len(calls) == 1:
        return [await execute_tool(*calls[0])]
    return list(await asyncio.gather(*(execute_tool(n, a) for n, a in calls)))


def _decode_args(fn: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments arrive as a JSON *string*, and a model can emit a broken
    one. An empty dict lets the tool fail on its own terms rather than taking
    the whole reply down."""
    raw = fn.get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "chat_tool_bad_arguments", tool=fn.get("name"), raw=str(raw)[:200]
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _complete_text_openai(
    config: LLMConfig,
    messages: list[dict[str, Any]],
    api_key: str,
    tools: list[dict[str, Any]] | None = None,
    execute_tool: ToolExecutor | None = None,
) -> CompletionResult:
    """Chat-completions call, with an optional tool loop.

    Without tools this is the single POST it has always been. With them, each
    round runs whatever the model asked for and feeds the results back until
    it answers in words or hits the cap.
    """
    base_url = _resolve_base_url(config)
    url = f"{base_url}/chat/completions"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {
        "model": config.model,
        # Copied: the loop appends to this, and the caller's history is theirs.
        "messages": list(messages),
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.reasoning_effort:
        # OpenAI reasoning models (o-series/gpt-5); ignored when unset.
        body["reasoning_effort"] = config.reasoning_effort
    if config.provider == "openrouter" and config.fallback_models:
        # OpenRouter's `models` array — tried in order, primary first.
        body["models"] = [config.model, *config.fallback_models]
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]

    client = get_http_client()
    total_tokens = 0

    async def _round() -> dict[str, Any]:
        nonlocal total_tokens
        response = await client.post(url, json=body, headers=headers, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        total_tokens += data.get("usage", {}).get("total_tokens", 0)
        return data["choices"][0]["message"]

    for _ in range(_MAX_TOOL_ROUNDS):
        message = await _round()
        calls = message.get("tool_calls") if execute_tool else None
        if not calls:
            return CompletionResult(
                text=message.get("content") or "", total_tokens=total_tokens
            )

        body["messages"].append(message)
        results = await _run_calls(
            execute_tool,
            [
                (
                    c.get("function", {}).get("name", ""),
                    _decode_args(c.get("function", {})),
                )
                for c in calls
            ],
        )
        body["messages"].extend(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            }
            for call, result in zip(calls, results, strict=True)
        )

    # Cap reached. Withhold the tools so the model has to answer in words: an
    # SMS with a mediocre answer beats an SMS that never arrives.
    logger.warning(
        "chat_tool_rounds_exhausted", model=config.model, rounds=_MAX_TOOL_ROUNDS
    )
    body.pop("tools", None)
    message = await _round()
    return CompletionResult(
        text=message.get("content") or "", total_tokens=total_tokens
    )


async def complete_text(
    config: LLMConfig,
    messages: list[dict[str, Any]],
    *,
    aws: AWSConfig | None = None,
    tools: list[dict[str, Any]] | None = None,
    execute_tool: ToolExecutor | None = None,
) -> CompletionResult:
    """Generate a text completion using the agent's LLM config.

    Supports: openai, ollama, custom_openai (BYOM), anthropic (Claude),
    openrouter, bedrock.

    `aws` carries the agent's credential block for the bedrock provider. Callers
    that hold an AgentConfig should pass it; omitting it falls back to the
    ambient boto3 chain, which is not the agent's configured principal.

    `tools` + `execute_tool` opt into function calling. Every provider here
    supports it, each in its own dialect. Omitting them sends exactly the
    request this function always sent — which callers like post-call analysis
    and the transfer briefing depend on, since a tool call there would be
    nonsense.

    Note: openrouter is blocked for customer SMS/Chat conversations at the
    sms_chat boundary, but allowed here for internal callers like post-call
    analysis. See ADR-0003.
    """

    api_key = _resolve_api_key(config)

    logger.debug(
        "llm_text_request",
        provider=config.provider,
        model=config.model,
        message_count=len(messages),
    )

    if config.provider == "bedrock":
        result = await _complete_text_bedrock(
            config, aws or AWSConfig(), messages, tools, execute_tool
        )
    elif config.provider == "anthropic":
        result = await _complete_text_anthropic(
            config, messages, api_key, tools, execute_tool
        )
    else:
        result = await _complete_text_openai(
            config, messages, api_key, tools, execute_tool
        )

    logger.debug(
        "llm_text_response",
        provider=config.provider,
        model=config.model,
        tokens=result.total_tokens,
        reply_length=len(result.text),
    )

    return result
