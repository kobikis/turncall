"""gpt-live-1's voice, as the Avatar reads a reply (#401).

The Avatar reads the agent's reply as TTS audio, which TTS and every other
realtime service hand out. gpt-live-1 streams speech instead
(`SpeechOutputAudioRawFrame`), silence included, at real-time pace: the Avatar
would read none of it, and pass the whole stream on to the output beside the
audio of its own leg, two real-time streams into one line.

So directly before the Avatar its reply is cut out of the stream: speech, and
the pauses inside it up to pipecat's own stop window (`BOT_VAD_STOP_SECS`), go
on as TTS audio; a longer silence ends the reply with a `TTSStoppedFrame`
(Tavus's end-of-utterance mark) and is dropped. TTS audio passes untouched, so
on any other service this does nothing. An Avatar that fell back to voice
passes the reply on as TTS audio, which the output plays as it plays cascade's.
"""

from __future__ import annotations

from pipecat.audio.utils import is_silence
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    SpeechOutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_output import BOT_VAD_STOP_SECS


class SpeechForTheAvatar(FrameProcessor):
    """A streamed voice's replies, as the TTS audio an Avatar reads."""

    def __init__(self) -> None:
        super().__init__()
        self._speaking = False
        self._silence_secs = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # The reply it cut is over; what follows is not the rest of it.
            self._speaking, self._silence_secs = False, 0.0
        if not isinstance(frame, SpeechOutputAudioRawFrame):
            await self.push_frame(frame, direction)
            return
        if not is_silence(frame.audio):
            self._speaking, self._silence_secs = True, 0.0
        elif self._speaking:
            self._silence_secs += len(frame.audio) / (
                2 * frame.sample_rate * frame.num_channels
            )
            if self._silence_secs > BOT_VAD_STOP_SECS:
                self._speaking = False
                await self.push_frame(TTSStoppedFrame(), direction)
        if not self._speaking:
            return
        await self.push_frame(
            TTSAudioRawFrame(
                audio=frame.audio,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            ),
            direction,
        )
