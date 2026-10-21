"""Warm the LLM before the caller's first turn needs it (`llm.warm_up`).

On the website demo the first reply's LLM TTFB was ~1.0s against ~0.6s for
every later one (gpt-4o-mini). The first request pays for what the later ones
reuse: the OpenAI client's lazy setup, DNS and TLS to the API, and whatever
the provider keeps warm for a prompt it has just seen.

So when the caller starts their first turn, the call's own LLM client is sent a
one-token completion with the agent's system prompt, while they are still
talking. Not at call start: the OpenAI client drops a connection idle for 5s,
and a greeting plus the caller's first sentence is often longer than that.

Opt-in because it is a paid request on every call that speaks: the system
prompt's tokens once more, and one output token. OpenAI-compatible services
only (openai, openrouter, custom_openai, ollama, xai); on any other the flag is
logged and ignored.
"""

import asyncio
from typing import Any

from loguru import logger

_TIMEOUT_SECONDS = 5.0


async def warm_up(llm: Any) -> None:
    """One throwaway completion on `llm`'s own client. Never raises: a failed
    warm-up costs the call nothing it had before."""
    llm = getattr(llm, "active_llm", llm)  # a failover group: its live member
    client = getattr(llm, "_client", None)
    settings = getattr(llm, "_settings", None)
    if not hasattr(client, "chat") or settings is None:
        logger.info("llm_warm_up_unsupported: {llm}", llm=type(llm).__name__)
        return
    prompt = getattr(settings, "system_instruction", None)
    if not isinstance(prompt, str) or not prompt:
        prompt = "You are a helpful assistant."
    try:
        await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Hi"},
                ],
                max_completion_tokens=1,
            ),
            _TIMEOUT_SECONDS,
        )
    except Exception as exc:  # the connection is warm either way
        logger.debug("llm_warm_up_failed: {error}", error=exc)


def warm_up_on_first_turn(user_aggregator: Any, llm: Any) -> None:
    """Send the warm-up when the caller's first turn starts."""
    fired = False

    @user_aggregator.event_handler("on_user_turn_started")  # type: ignore[misc]
    async def _first_turn(*_args: object) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        await warm_up(llm)
