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
from dataclasses import dataclass
from typing import Any

from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.config.settings import get_settings
from turncall.domain.models import AWSConfig, LLMConfig


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


async def _complete_text_anthropic(
    config: LLMConfig,
    messages: list[dict[str, str]],
    api_key: str,
) -> CompletionResult:
    """Generate a text completion via the Anthropic Messages API."""
    # Separate system messages from conversation messages
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
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
    body: dict = {
        "model": config.model,
        "messages": conversation,
        "max_tokens": config.max_tokens,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)

    client = get_http_client()
    response = await client.post(url, json=body, headers=headers, timeout=30.0)
    response.raise_for_status()

    data = response.json()
    reply_text = data["content"][0]["text"]
    usage = data.get("usage", {})
    total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    return CompletionResult(text=reply_text, total_tokens=total_tokens)


async def _complete_text_bedrock(
    config: LLMConfig,
    aws: AWSConfig,
    messages: list[dict[str, str]],
) -> CompletionResult:
    """Generate a text completion via the Bedrock converse API (ADR-0016)."""
    import boto3

    from turncall.services.aws_credentials import resolve_aws_credentials

    credentials = resolve_aws_credentials(aws)

    # converse() takes system prompts out of band, like the Anthropic path.
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    conversation = [
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

    def _invoke() -> dict[str, Any]:
        client = boto3.client(
            "bedrock-runtime",
            region_name=credentials.region,
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
        )
        return client.converse(**kwargs)

    try:
        # boto3 is synchronous; keep it off the event loop.
        response = await asyncio.to_thread(_invoke)
    except Exception as exc:
        # Bedrock availability is region-specific and the raw error names
        # neither side of the pair that failed. ADR-0016.
        msg = (
            f"Bedrock converse failed for model {config.model!r} in region "
            f"{credentials.region!r}: {exc}"
        )
        raise RuntimeError(msg) from exc

    text = response["output"]["message"]["content"][0]["text"]
    total_tokens = response.get("usage", {}).get("totalTokens", 0)
    return CompletionResult(text=text, total_tokens=total_tokens)


async def complete_text(
    config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    aws: AWSConfig | None = None,
) -> CompletionResult:
    """Generate a text completion using the agent's LLM config.

    Supports: openai, ollama, custom_openai (BYOM), anthropic (Claude),
    openrouter, bedrock.

    `aws` carries the agent's credential block for the bedrock provider. Callers
    that hold an AgentConfig should pass it; omitting it falls back to the
    ambient boto3 chain, which is not the agent's configured principal.

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
        result = await _complete_text_bedrock(config, aws or AWSConfig(), messages)
    elif config.provider == "anthropic":
        result = await _complete_text_anthropic(config, messages, api_key)
    else:
        base_url = _resolve_base_url(config)
        url = f"{base_url}/chat/completions"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if config.reasoning_effort:
            # OpenAI reasoning models (o-series/gpt-5); ignored when unset.
            body["reasoning_effort"] = config.reasoning_effort
        if config.provider == "openrouter" and config.fallback_models:
            # OpenRouter's `models` array — tried in order, primary first.
            body["models"] = [config.model, *config.fallback_models]

        client = get_http_client()
        response = await client.post(url, json=body, headers=headers, timeout=30.0)
        response.raise_for_status()

        data = response.json()
        reply_text = data["choices"][0]["message"]["content"]
        total_tokens = data.get("usage", {}).get("total_tokens", 0)
        result = CompletionResult(text=reply_text, total_tokens=total_tokens)

    logger.debug(
        "llm_text_response",
        provider=config.provider,
        model=config.model,
        tokens=result.total_tokens,
        reply_length=len(result.text),
    )

    return result
