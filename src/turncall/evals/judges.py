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
#
# What a **simulator** may be. A persona is linked into a pipeline to play the
# caller, so every entry here has to be a real LLM service.
PROVIDERS: dict[str, str] = {
    "ollama": "turncall.evals.judges.ollama",
    "openai": "turncall.evals.judges.openai",
    "anthropic": "turncall.evals.judges.anthropic",
}

# What a **judge** may be — a superset, because a judge only has to answer
# questions and never joins a pipeline. Pipecat accepts either an LLM service
# or a `BaseClassifier` here (`classifier_from_config`).
#
# `ollama` is the one that differs: the same local LLM, wrapped so its budget
# can be widened. Handing that wrapper to a simulator raises
# `AttributeError: 'LLMClassifier' object has no attribute 'link'` at pipeline
# build, which is why the two roles get two maps rather than one with a check.
JUDGE_PROVIDERS: dict[str, str] = {
    **PROVIDERS,
    "ollama": "turncall.evals.judges.ollama_judge",
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
PROVIDER_BY_FACTORY: dict[str, str] = {
    path: name for name, path in {**PROVIDERS, **JUDGE_PROVIDERS}.items()
}


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


# How long a local judge gets to answer one classification.
#
# pipecat 1.12 judges a simulation **one bot turn per call** rather than the
# whole run in one prose call, and fires those calls together. A local Ollama
# serializes them, so the per-call latency scales with the turn count while
# `LLMClassifier`'s own default budget stays at 10s. Measured on this box with
# `gemma4:e2b`: one classification 4.5-5.8s, four concurrent 18s each — so the
# default judge timed out on every turn of a four-turn simulation, which the
# runner correctly scored as a judge failure and which looks exactly like an
# agent regression from the outside.
#
# 60s is generous against that 18s and still a third of the 180s floor the
# per-iteration budget gives one conversation (ADR-0018), so a hung judge is
# still caught by the budget rather than by this.
_LOCAL_JUDGE_TIMEOUT_S = 60.0


def ollama(config: dict[str, Any]) -> Any:
    """Pipecat's own local LLM, reached through the same door as the rest.

    The persona's, and the judge's before 1.12 gave the judge a reason to want
    a wrapper. See `ollama_judge`.
    """
    from pipecat.evals.services import ollama_service

    return ollama_service(config)


def ollama_judge(config: dict[str, Any]) -> Any:
    """The same local LLM, wrapped so the judge's budget can be widened.

    The one factory that returns a classifier rather than an LLM service.
    Pipecat accepts either (`classifier_from_config`), and wrapping it here
    leaves explainer resolution identical: `EvalJudge.from_config` reads
    `classifier.llm` for an `LLMClassifier` that came without an `explainer:`
    block, which is the same Ollama the bare service would have been.
    """
    from pipecat.classifiers.llm.classifier import LLMClassifier

    return LLMClassifier(llm=ollama(config), timeout=_LOCAL_JUDGE_TIMEOUT_S)


def _platform_key(attr: str) -> str:
    """The provider's key from TurnCall's own settings.

    Never from the scenario: a judge block arrives in an API body, and a
    credential that can be *set* there is one that gets stored in JSONB,
    returned by a read, and masked forever after (#91). The platform holds the
    keys for the agent's own LLM already, and the judge is the same trust
    boundary.
    """
    from turncall.config.settings import get_settings

    return getattr(get_settings(), attr).api_key


def openai(config: dict[str, Any]) -> Any:
    """An OpenAI judge. `extra` carries whatever the model takes, temperature
    included — pipecat forwards it as top-level request parameters."""
    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService(
        api_key=_platform_key("openai"),
        # An OpenAI-compatible gateway, when the scenario named one. The same
        # field carries Ollama's URL, which is why it is spelled `endpoint`
        # rather than after either vendor.
        base_url=config.get("endpoint") or None,
        settings=OpenAILLMService.Settings(
            model=config.get("model") or DEFAULT_MODELS["openai"],
            extra=dict(config.get("extra") or {}),
        ),
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
        # Required by name on this one — pipecat's Anthropic service takes no
        # key from the environment, and omitting it raised `TypeError` on the
        # first judged run rather than at construction time.
        api_key=_platform_key("anthropic"),
        settings=AnthropicLLMService.Settings(
            model=config.get("model") or DEFAULT_MODELS["anthropic"],
            extra=dict(config.get("extra") or {}),
        ),
    )
