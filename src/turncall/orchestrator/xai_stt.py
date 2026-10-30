"""xAI speech-to-text as TurnCall builds it (#394).

Imports pipecat at module level, so `pipeline_factory` imports it only when it
builds an `xai` agent's STT.
"""

import json
from typing import Any
from urllib.parse import urlencode

from loguru import logger
from pipecat.frames.frames import Frame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.xai.stt import XAISTTService
from websockets.protocol import State

from turncall.orchestrator.xai_errors import XAIKeyRefusal

_FINALIZE = json.dumps({"type": "finalize"})

# `stt.extra` keys pipecat has a setting for, and so sends itself.
_SETTING_KEYS = ("endpointing", "interim_results", "diarize")
# Query parameters xAI takes that pipecat has no setting for. `smart_turn` is
# xAI's own end-of-turn model, a second opinion on what TurnCall's VAD and
# Smart Turn already decide: reachable, not recommended.
_URL_KEYS = (
    "format",
    "filler_words",
    "vad_threshold",
    "smart_turn",
    "smart_turn_timeout",
)


class TurnCallXAISTT(XAIKeyRefusal, XAISTTService):
    """xAI STT, reading xAI's refusal of a key as one (`XAIKeyRefusal`).

    Pipecat builds the connection URL from its declared settings alone, so
    what it has no setting for — `keyterm`, repeated once per term, and the
    URL-only `stt.extra` keys — is added to the URL here.
    """

    def __init__(self, *, url_params: list[tuple[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._url_params = url_params

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Ask for the caller's final transcript the moment VAD hears them
        stop, as pipecat's Deepgram does. Otherwise xAI holds it until its own
        endpointing silence: measured on 8 kHz, 0.86 s after speech by default,
        0.23 s with `finalize`.

        No `request_finalize()`: xAI confirms nothing, and pipecat already
        marks its `speech_final` transcript finalized.
        """
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._send_finalize()

    async def _send_finalize(self) -> None:
        socket = self._websocket
        if not (socket and socket.state is State.OPEN and self._session_ready.is_set()):
            return
        try:
            await socket.send(_FINALIZE)
        except Exception as e:
            await self.push_error(
                error_msg=f"xAI STT finalize failed: {e}", exception=e
            )

    def _build_ws_url(self) -> str:
        url: str = super()._build_ws_url()
        if not self._url_params:
            return url
        return f"{url}&{urlencode(self._url_params)}"


# Nothing xAI sends once the call is ending is read.
_CLOSE_TIMEOUT_SECS = 0.25


def create_xai_stt(
    *,
    api_key: str,
    model: str,
    language: str,
    keyterms: list[str],
    extra: dict[str, Any],
    sample_rate: int,
) -> TurnCallXAISTT:
    ignored = sorted(set(extra) - {*_SETTING_KEYS, *_URL_KEYS})
    if ignored:
        logger.warning("xai_stt_extra_ignored keys={keys}", keys=ignored)
    settings = {k: extra[k] for k in _SETTING_KEYS if k in extra}
    url_extra = [(k, _query_value(extra[k])) for k in _URL_KEYS if k in extra]
    return TurnCallXAISTT(
        api_key=api_key,
        sample_rate=sample_rate,
        settings=TurnCallXAISTT.Settings(model=model, language=language, **settings),
        url_params=[("keyterm", term) for term in keyterms] + url_extra,
        # xAI never answers the close handshake: pipecat's 2 s default held
        # every call's end that long after the goodbye.
        ws_close_timeout=_CLOSE_TIMEOUT_SECS,
    )


def _query_value(value: Any) -> Any:
    """Booleans as xAI spells them, as pipecat does for its own settings."""
    return str(value).lower() if isinstance(value, bool) else value
