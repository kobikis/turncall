"""Eleven Realtime's pipecat service as TurnCall builds it (#465).

pipecat's OpenAI Realtime service, pointed at ElevenLabs. Imports pipecat at
module level, so `s2s_config` imports it only when it builds an `elevenlabs`
agent's service; `eleven_realtime` is the half the API schema reads.
"""

from typing import Any

from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.utils.errors import ErrorCategory
from websockets.exceptions import ConnectionClosedError

# ElevenLabs accepts the socket with a wrong key, then closes it with this.
_KEY_REFUSED_CLOSE = 4001


class ElevenRealtime(OpenAIRealtimeLLMService):
    """`session` holds what ElevenLabs' session takes and pipecat's
    SessionProperties has no field for (temperature, the session length, the
    transcription language), added to every `session.update`."""

    # What `call_session.provider_of` names, rather than the `openai` module
    # this service comes from.
    vendor = "elevenlabs"

    def __init__(self, *, session: dict[str, Any], language: str | None, **kwargs):
        super().__init__(**kwargs)
        self._session_extras = session
        self._language = language

    async def _auth_headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key}

    async def send_client_event(self, event: events.ClientEvent) -> None:
        if not isinstance(event, events.SessionUpdateEvent):
            await super().send_client_event(event)
            return
        message = event.model_dump(exclude_none=True)
        session = message["session"]
        session.update(self._session_extras)
        if self._language:
            # Input is always transcribed; only the language is ElevenLabs'.
            session["audio"]["input"]["transcription"] = {"language": self._language}
        await self._ws_send(message)

    async def _receive_task_handler(self) -> None:
        """pipecat's receive loop only logs a closed socket, which would leave
        the call silent; here it ends the service, naming a refused key."""
        try:
            await super()._receive_task_handler()
        except ConnectionClosedError as closed:
            refused = closed.rcvd is not None and closed.rcvd.code == _KEY_REFUSED_CLOSE
            await self.push_error(
                f"ElevenLabs closed the session: {closed}",
                exception=closed,
                category=ErrorCategory.AUTHENTICATION if refused else None,
                force_treat_as_permanent=True,
            )
