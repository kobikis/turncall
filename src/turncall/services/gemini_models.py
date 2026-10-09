"""Which sampling settings a Gemini model is sent.

Google, on every Gemini 3 model: "we strongly recommend keeping the
temperature parameter at its default value of 1.0. Changing the temperature
(setting it below 1.0) may lead to unexpected behavior, such as looping or
degraded performance". TurnCall's 0.7 is its own default rather than a value
the agent asked for — `model_dump()` persisted it into every agent — so sending
it would put each Gemini 3 agent where Google says not to be, for a knob nobody
set. The model's own 1.0 applies instead, and `llm.extra` is how an agent sets
one anyway. Read by both routes: the voice pipeline and the text path.
"""

from __future__ import annotations

import re

# The major version in every shape a Gemini id comes in: `gemini-2.5-flash`,
# `gemini-3-flash-preview`, `models/gemini-3.6-flash`.
_GEMINI_MAJOR = re.compile(r"gemini(?:-[a-z]+)*-(\d+)")


def takes_temperature(model: str) -> bool:
    """Whether the agent's temperature is sent to this Gemini model.

    Only a model that names a version before 3. An unversioned alias
    (`gemini-flash-latest`) points at Gemini 3 today, so it is treated as one.
    """
    match = _GEMINI_MAJOR.search(model or "")
    return match is not None and int(match.group(1)) < 3
