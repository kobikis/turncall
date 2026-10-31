"""What an OpenAI model is sent on Chat Completions.

TurnCall used to send every model `temperature`, `max_tokens` and the agent's
`reasoning_effort`, and three model families each refuse one of them with a
400 that ends the call on its first reply. Measured on 2026-10-30:

- `max_tokens` is refused by the gpt-5 family ("use max_completion_tokens"),
  which every model takes, so `max_completion_tokens` is sent instead.
- `reasoning_effort` is refused by a model that does not reason (gpt-4o,
  gpt-4.1: "Unrecognized request argument").
- `temperature` other than 1 is refused by a model that always reasons (the
  o-series, gpt-5/-mini/-nano), and by gpt-5.1 and later while they reason:
  they take one only at their default effort, none.
- gpt-5.1 and later refuse `minimal` (their least is `none`, their default),
  and the o-series refuse it too (its least is `low`).
- gpt-5.4 and later refuse any effort beside function tools on Chat
  Completions. A voice call can gain tools mid-call (a skill, a handoff, MCP),
  so on voice their effort is never sent.

A value the model refuses is dropped, never the call: the model's default
applies and `dropped` names the effort that went, for the caller to log. Only the
`openai` provider: an OpenAI-compatible endpoint is someone else's model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `gpt-5`, `gpt-5-mini`, `gpt-5.4-nano-2026-03-05`: the major and minor version.
_GPT = re.compile(r"^gpt-(\d+)(?:\.(\d+))?")
_O_SERIES = re.compile(r"^o\d")


@dataclass(frozen=True)
class OpenAIParams:
    temperature: float | None
    max_completion_tokens: int
    reasoning_effort: str | None
    dropped: tuple[str, ...] = ()


def _version(model: str) -> tuple[int, int] | None:
    match = _GPT.match(model)
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def always_reasons(model: str) -> bool:
    """Whether `model` reasons before it answers with no effort sent: the
    o-series, and gpt-5.0, which has no `none`."""
    name = (model or "").lower()
    return _O_SERIES.match(name) is not None or _version(name) == (5, 0)


def chat_params(
    model: str,
    *,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None,
    tools: bool,
) -> OpenAIParams:
    """The sampling settings `model` takes. `tools`: whether the request has
    function tools, or (voice) may come to."""
    name = (model or "").lower()
    version = _version(name)
    o_series = _O_SERIES.match(name) is not None
    reasons = o_series or (version is not None and version >= (5, 0))

    dropped: list[str] = []
    effort = reasoning_effort
    if effort and not reasons:
        dropped.append(f"reasoning_effort ({model} does not reason)")
        effort = None
    elif effort == "minimal" and version != (5, 0):
        dropped.append(f"reasoning_effort minimal ({model} does not take it)")
        effort = None
    elif effort and tools and version is not None and version >= (5, 4):
        dropped.append(
            f"reasoning_effort ({model} takes none beside function tools "
            "on Chat Completions)"
        )
        effort = None

    # Not reported: TurnCall's 0.7 is its own default, which every agent
    # carries, so the drop would be news on every call (as for Claude).
    sends_temperature = not always_reasons(name) and not effort
    return OpenAIParams(
        temperature=temperature if sends_temperature else None,
        max_completion_tokens=max_tokens,
        reasoning_effort=effort,
        dropped=tuple(dropped),
    )
