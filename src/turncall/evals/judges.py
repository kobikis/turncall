"""The LLMs an eval runs besides the agent's: the judge, and a persona (#118).

Pipecat builds exactly one of these natively. `llm_service_from_config` knows
`ollama` and a deprecated `openai`, and routes everything else through
`factory` — a dotted path it hands to `importlib.import_module`. That is fine
for a scenario file on someone's disk and unusable for a scenario arriving in
an API body: importing a caller-supplied module is remote code execution.

So TurnCall ships the factories instead. A request names a **provider** from a
closed set, and `PROVIDERS` maps it to one of the callables below; a dotted
path is never read from a request. That also outlives pipecat 2.0, which
removes `service: openai` entirely.

Each callable takes pipecat's own config mapping and returns a service with
`run_inference()`, which is the whole contract a judge or a persona needs.
"""

from __future__ import annotations

from typing import Any

# provider -> the dotted path pipecat will import. The values name callables in
# this module and nothing else: the mapping is the allowlist.
PROVIDERS: dict[str, str] = {
    "ollama": "turncall.evals.judges.ollama",
    "openai": "turncall.evals.judges.openai",
    "anthropic": "turncall.evals.judges.anthropic",
}

# What each provider runs when a scenario names no model. Pipecat's own default
# judge is a local Ollama; the hosted ones pick a small, cheap model, because a
# judge reads one reply and answers a yes/no question.
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}


# The same mapping read backwards, so a compiled block can say which provider
# produced it. `harness_config` records the provider a run was judged by, and
# by then the typed block is gone — pipecat stores the dotted path.
PROVIDER_BY_FACTORY: dict[str, str] = {path: name for name, path in PROVIDERS.items()}


def default_block(
    provider: Any = None, model: str | None = None, temperature: float | None = None
) -> dict[str, Any] | None:
    """A platform-wide judge/simulator block, or None when none is configured (#119).

    Taken whole or not at all: a scenario that names a judge keeps the one it
    names. Merging field by field would let a platform `model` land on a
    provider that has never heard of it.

    A model or a temperature with no provider means "the default provider,
    configured" — `compile_model_block` fills in ollama, which is what pipecat
    would have run regardless.
    """
    if provider is None and model is None and temperature is None:
        return None
    return {
        "provider": str(provider) if provider is not None else "ollama",
        "model": model,
        "temperature": temperature,
    }


def ollama(config: dict[str, Any]) -> Any:
    """Pipecat's own local judge, reached through the same door as the rest."""
    from pipecat.evals.services import ollama_service

    return ollama_service(config)


def openai(config: dict[str, Any]) -> Any:
    """An OpenAI judge. `extra` carries whatever the model takes, temperature
    included — pipecat forwards it as top-level request parameters."""
    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService(
        settings=OpenAILLMService.Settings(
            model=config.get("model") or DEFAULT_MODELS["openai"],
            extra=dict(config.get("extra") or {}),
        )
    )


def anthropic(config: dict[str, Any]) -> Any:
    """A Claude judge.

    No temperature reaches this one — `compile_model_block` drops it before the
    config is built, because current Claude models reject the parameter
    outright (`400 temperature is deprecated for this model`). The rule lives
    with the model rather than the caller, exactly as it does on the call path.
    """
    from pipecat.services.anthropic.llm import AnthropicLLMService

    return AnthropicLLMService(
        settings=AnthropicLLMService.Settings(
            model=config.get("model") or DEFAULT_MODELS["anthropic"],
            extra=dict(config.get("extra") or {}),
        )
    )
