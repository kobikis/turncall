"""The per-turn context message (#441): guidance for one caller turn only.

Something that has a word for this turn — KB auto mode's passages, the
procedure window (#442) — puts one `developer` message right before the
caller's turn and takes out the one it put before the previous turn, or
every turn's would stay in the prompt for good. The context is the call's
history, shared with the aggregators, so this is one shared replace rather
than a copy per processor.

A marker — the text the message starts with — says whose message it is, so
two of them in one context never take out each other's.
"""

from collections.abc import Awaitable, Callable
from typing import Any

# (the context's messages without this marker's, the caller's turn text)
# -> this turn's message, or None for none. The message must start with the
# marker, or the next turn cannot find it to take out.
MakeTurnMessage = Callable[[list[Any], str], Awaitable[dict[str, Any] | None]]


def text_of(message: Any) -> str:
    """A message's text, whether its content is a string or content parts."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        return " ".join(t for t in parts if t).strip()
    return ""


def is_marked(message: Any, marker: str) -> bool:
    return isinstance(message, dict) and text_of(message).startswith(marker)


async def replace_turn_message(
    context: Any, marker: str, make: MakeTurnMessage
) -> None:
    """Take out the previous turn's `marker` message and, when the context
    ends on the caller's turn and `make` has one, put this turn's right
    before that turn."""
    messages = [m for m in context.get_messages() if not is_marked(m, marker)]
    last = messages[-1] if messages else None
    current = (
        text_of(last) if isinstance(last, dict) and last.get("role") == "user" else ""
    )
    made = await make(messages, current) if current else None
    # Read again: the aggregators share this context and may have added to
    # it while `make` was out — writing back the list read before it would
    # drop that. The turn is found by identity, not position.
    fresh = [m for m in context.get_messages() if not is_marked(m, marker)]
    at = next((i for i, m in enumerate(fresh) if m is last), None)
    if made is not None and at is not None:
        fresh = [*fresh[:at], made, *fresh[at:]]
    context.set_messages(fresh)
