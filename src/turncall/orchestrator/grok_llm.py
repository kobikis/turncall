"""Grok's text models as TurnCall builds them (#393).

Imports pipecat at module level, so the pipeline factory imports it only when
it builds an `xai` agent's LLM.
"""

from pipecat.services.xai.llm import GrokLLMService

from turncall.orchestrator.xai_errors import XAIKeyRefusal


class KeyAwareGrokLLM(XAIKeyRefusal, GrokLLMService):
    """Grok, reading xAI's refusal of a key as one (`XAIKeyRefusal`)."""
