"""Which model an LLM provider gets when the agent named none.

`LLMConfig.model` defaulted to `gpt-4o-mini` — OpenAI's model — in a
provider-agnostic field, so an agent that named no model sent it to whichever
provider it had chosen:

    anthropic   404 not_found_error: model: gpt-4o-mini

The same shape as `_stt_model` in the pipeline factory and `_tts_model_voice`
beside it. This one lives in services rather than the orchestrator because
three callers need it and two of them are not pipeline code: the voice
pipeline, the chat/SMS text path, and post-call analysis.

Defaults exist only where there is a house model to fall back to. The other
four providers *are* a model choice — a Bedrock id's availability is
region-specific (ADR-0016), an Ollama model is whatever is pulled onto that
box, a custom OpenAI-compatible endpoint decides for itself, and OpenRouter
exists precisely to pick among vendors. Guessing for them would trade a clear
error for a confusing one, so they raise instead.
"""

from __future__ import annotations

# `LLMConfig.model`'s old default. Read as "unset" on every provider but the
# one it belongs to, because `model_dump()` persisted it into the config_blob
# of every agent created before it was cleared.
_LEGACY_OPENAI_SENTINEL = "gpt-4o-mini"

_LLM_DEFAULTS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
}

# Providers where the model is the deployment, not a preference.
_MODEL_REQUIRED = ("ollama", "custom_openai", "bedrock", "openrouter")

_WHY_REQUIRED = {
    "ollama": "the model is whichever one is pulled onto that Ollama host",
    "custom_openai": "the endpoint decides what it serves",
    "bedrock": "Bedrock model availability is region-specific (see adr/0016)",
    "openrouter": (
        "OpenRouter routes between vendors, so the id carries one "
        "(e.g. 'anthropic/claude-sonnet-5')"
    ),
}


def model_is_required(provider: str) -> bool:
    """Whether this provider has no house model to fall back to."""
    return provider in _MODEL_REQUIRED


def resolve_llm_model(provider: str, configured: str) -> str:
    """The model to send, resolving an unset or legacy-sentinel value.

    Args:
        provider: the agent's LLM provider.
        configured: whatever `LLMConfig.model` holds.

    Returns:
        The model to send. A value the agent actually chose is returned
        untouched, so a wrong one is reported by the provider rather than
        silently replaced (adr/0016).

    Raises:
        ValueError: the provider has no sensible default and none was given.
    """
    unset = not configured or (
        configured == _LEGACY_OPENAI_SENTINEL and provider != "openai"
    )
    if not unset:
        return configured

    if provider in _LLM_DEFAULTS:
        return _LLM_DEFAULTS[provider]

    if provider in _MODEL_REQUIRED:
        raise ValueError(
            f"llm.model is required for provider '{provider}': "
            f"{_WHY_REQUIRED[provider]}."
        )

    return configured
