"""Grok's text models as TurnCall builds them (#393).

Imports pipecat at module level, so the pipeline factory imports it only when
it builds an `xai` agent's LLM.
"""

from pipecat.services.xai.llm import GrokLLMService

from turncall.domain.models import LLMConfig
from turncall.orchestrator.xai_config import llm_refusal
from turncall.orchestrator.xai_errors import XAIKeyRefusal


class KeyAwareGrokLLM(XAIKeyRefusal, GrokLLMService):
    """Grok, reading xAI's refusal of a key as one (`XAIKeyRefusal`)."""


def create_grok_llm(
    llm: LLMConfig,
    *,
    api_key: str | None,
    model: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None,
    system_instruction: str,
) -> KeyAwareGrokLLM:
    """The voice call's Grok, on the xAI account's key and xAI's endpoint.

    The API schema refuses what `llm_refusal` names at save; a call-init inline
    agent reaches here unchecked, so the build refuses it too. Reasoning effort
    is pipecat's own setting, never `extra_body`, so the two cannot compete;
    None sends nothing, since Grok's non-reasoning models reject the parameter
    outright.
    """
    problem = llm_refusal(llm)
    if problem:
        raise ValueError(problem)
    si = {"system_instruction": system_instruction} if system_instruction else {}
    return KeyAwareGrokLLM(
        api_key=api_key,
        settings=KeyAwareGrokLLM.Settings(
            model=model,
            **si,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort or None,
        ),
    )
