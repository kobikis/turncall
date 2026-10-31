"""A hosted OpenAI-family chat completion that never starts answering is sent
again, as many times as it takes, each one logged.

Measured 2026-10-31 from this deployment and from a laptop alike: about one
request in ten to twenty to `gpt-5.4-mini` is sent whole and gets no response
headers for 30 s or more, while the same request sent again answers in under
a second. Three calls (c02c84fd twice, ebfa5e53) sat 16-17 s in silence on
one, until the caller's "Hello?" cancelled it. pipecat's `retry_on_timeout`
re-sends once with no limit, which ebfa5e53 ran with and still sat 16 s, and
logs the retry at DEBUG, where nobody sees it.
"""

import asyncio

from loguru import logger
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

# How long a request may go without its response headers. They arrive with
# the first token, so this is also the longest first token allowed; a normal
# one here is under 1.2 s. A model that reasons first may take longer, so it
# is not given this service (`pipeline_factory._openai_llm_class`).
LLM_STALL_SECONDS = 5.0
# Attempts cut at LLM_STALL_SECONDS before the last, which waits as long as
# it takes: a stall costs the caller 5 s, two in a row 10 s, and a third in a
# row is waited out. Rare if stalls are independent (measured one in 10-20);
# a burst while OpenAI is degraded can make it common.
_CUT_ATTEMPTS = 2


class StallRetryingOpenAILLMService(OpenAILLMService):
    """pipecat's OpenAI service, whose request is cut and sent again while no
    response headers come. pipecat awaits the headers in `get_chat_completions`
    and reads the stream later, so the cut never lands mid-answer."""

    async def get_chat_completions(
        self, context: LLMContext
    ) -> AsyncStream[ChatCompletionChunk]:
        # ponytail: each attempt re-reads the context and logs it again; one
        # request in ten to twenty, so not worth copying pipecat's body here.
        for attempt in range(1, _CUT_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(
                    super().get_chat_completions(context), LLM_STALL_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "llm_request_stalled: no answer in {secs}s, sent again "
                    "(attempt {attempt})",
                    secs=LLM_STALL_SECONDS,
                    attempt=attempt,
                )
        return await super().get_chat_completions(context)
