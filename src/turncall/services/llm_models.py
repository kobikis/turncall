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

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from loguru import logger

# `LLMConfig.model`'s old default. Read as "unset" on every provider but the
# one it belongs to, because `model_dump()` persisted it into the config_blob
# of every agent created before it was cleared.
_LEGACY_OPENAI_SENTINEL = "gpt-4o-mini"

_LLM_DEFAULTS: dict[str, str] = {
    # Not gpt-4o-mini, which OpenAI shuts down on 2027-01-14. gpt-5.4-mini
    # reasons only when asked (its default effort is none), so it answers a
    # voice turn without a thinking delay, and takes every setting TurnCall
    # sends it (services/openai_models.py).
    "openai": "gpt-5.4-mini",
    "anthropic": "claude-sonnet-5",
    # pipecat's own default for its Google service, and the fastest current
    # Flash that still takes the `minimal` thinking level pipecat sets for a
    # voice turn (3.7 and 3.8 Flash bottom out at `low`).
    "google": "gemini-3.6-flash",
    # pipecat's own default for Grok: no reasoning delay, and the price of
    # grok-4.3, which matches its latency only at `reasoning_effort: none`.
    "xai": "grok-4.20-non-reasoning",
}


@dataclass(frozen=True)
class Retirement:
    retires_on: date
    replacement: str


# (provider, model) -> when the vendor shuts it down and what runs from then
# (#458). Keyed on the provider because the same name elsewhere is something
# else: off OpenAI `gpt-4o-mini` is the old default read as unset, and on
# `custom_openai` the endpoint serves what it serves. The next one is a row.
_RETIREMENTS: dict[tuple[str, str], Retirement] = {
    # Every OpenAI agent created before `LLMConfig.model`'s default was cleared
    # stores it, and nothing tells those from one that chose it. A dead call is
    # worse than a different model.
    ("openai", "gpt-4o-mini"): Retirement(date(2027, 1, 14), _LLM_DEFAULTS["openai"]),
}


def _today() -> date:
    return datetime.now(UTC).date()


def retirement(provider: str, model: str | None) -> Retirement | None:
    """When this provider retires this model, or None if it has no date."""
    return _RETIREMENTS.get((provider, model or ""))


def retiring_warning(
    field: str, provider: str, model: str | None
) -> dict[str, Any] | None:
    """A read's `model_retiring` warning for a model with a retirement date.

    Derived on every read and never stored, like the agent's other warnings
    (#148), so it goes away the moment the author names another model.
    """
    retiring = retirement(provider, model)
    if retiring is None:
        return None
    return {
        "code": "model_retiring",
        "message": (
            f"{field}: {provider} retires '{model}' on "
            f"{retiring.retires_on.isoformat()}. From that date "
            f"'{retiring.replacement}' runs in its place. Name a current model "
            "to choose what runs."
        ),
        "model": model,
        "retires_on": retiring.retires_on.isoformat(),
        "replacement": retiring.replacement,
        "field": field,
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


def resolve_llm_model(
    provider: str, configured: str, *, today: date | None = None
) -> str:
    """The model to send, resolving an unset or legacy-sentinel value.

    Args:
        provider: the agent's LLM provider.
        configured: whatever `LLMConfig.model` holds.
        today: the clock, for tests.

    Returns:
        The model to send. A value the agent actually chose is returned
        untouched, so a wrong one is reported by the provider rather than
        silently replaced (adr/0016) — unless the vendor has retired it
        (`_RETIREMENTS`), where the call could only fail.

    Raises:
        ValueError: the provider has no sensible default and none was given.
    """
    unset = not configured or (
        configured == _LEGACY_OPENAI_SENTINEL and provider != "openai"
    )
    if not unset:
        return _unretired(provider, configured, today or _today())

    if provider in _LLM_DEFAULTS:
        return _LLM_DEFAULTS[provider]

    if provider in _MODEL_REQUIRED:
        raise ValueError(
            f"llm.model is required for provider '{provider}': "
            f"{_WHY_REQUIRED[provider]}."
        )

    return configured


def _unretired(provider: str, model: str, today: date) -> str:
    retiring = retirement(provider, model)
    if retiring is None or today < retiring.retires_on:
        return model
    logger.warning(
        "llm_model_retired from={model} to={replacement}",
        model=model,
        replacement=retiring.replacement,
    )
    return retiring.replacement
