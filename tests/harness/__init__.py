"""Seam A test harness: the running app, stub providers, a fake carrier (#214).

Drive TurnCall the way a carrier and a customer's backend do, and assert on
what they would observe — HTTP responses, the media stream, recorded carrier
REST actions, the call's events, signed webhooks — never on which internal
function ran. `tests/integration/test_harness_inbound_call.py` is the worked
example: one inbound Twilio call, webhook to `call.ended`.

Pieces
------
- `stubs.py` — `StubSTT`, `StubLLM`, `StubTTS`: real pipecat services with no
  network hop, scripted by a `Script`. The caller's n-th burst of audio
  transcribes as `Script.caller[n]`; the LLM's n-th inference answers
  `Script.agent[n]` (text, a `ToolCall`, or a tuple of both). A tool result is
  its own inference, so "call a tool, then say goodbye" is two entries.
  `install_stub_providers(script, set_attr)` swaps the three factories in
  `orchestrator/pipeline_factory` — cascade only; S2S, post-call analysis, the
  voicemail classifier and KB embeddings still reach real providers, so keep
  them off (`HARNESS_AGENT` does).
- `carrier.py` — `FakeCarrier`: owns numbers, records every REST action in
  `actions` (`dial`, `hang_up`, `transfer`, `send_dtmf`, `configure_number`,
  `configure_sms`, `send_sms`, and Telnyx's `answer`, `stop_streaming`,
  `speak`, `start_playback`, `stop_playback`, `bridge`), `wait_for(kind)` and
  `action_at(index)`.
  `install(set_attr)` replaces the adapters `services/twilio_accounts
  .TwilioAccount` and `services/telnyx_accounts.TelnyxAccount` hand out, which
  is every carrier REST call the app makes. It also holds the Telnyx account's
  API key and an Ed25519 keypair whose public half the app is given.
- `media_call.py` — `CarrierCall`, a live call's media stream from the
  caller's side: 20ms mulaw frames at real-time pace, silence unless
  `say(ms)`, `wait_for_agent_speech(n)`, `closed`, `hang_up()`, `finish()`,
  and `complete_transfer(answered_by=)` — the carrier's side of a transfer the
  app asked for, played to the end of the call, returning `TransferHeard`
  (what the caller and the operator heard, ringing, bridged).
- `twilio.py` — Twilio's edge: `sign`/`post_signed` for webhooks,
  `place_inbound_call` (POST the number's configured voice URL, open the
  `<Stream>`), and `TwilioCall` (`finish()` posts the `completed` status
  callback).
- `telnyx.py` — Telnyx's edge: Ed25519-signed Call Control webhooks
  (`sign`/`post_signed`/`event`), `bind_number`, `place_inbound_call` (post
  `call.initiated`, open the stream the app's `answer` names), and
  `TelnyxCall` (`finish()` posts `call.hangup`).
- `app.py` — `run_app(script, set_env=, set_attr=)`: the real app under uvicorn
  in the test's loop, a fresh project + admin key via the platform endpoints, a
  `WebhookSink` subscribed to `*` that verifies signatures. Skips when Postgres
  is unreachable or holds credentials this process cannot decrypt.

Extending it
------------
- Another carrier (ARI #209): a sibling of `twilio.py`/`telnyx.py` whose call
  is a `CarrierCall`, and the carrier's adapter routed to a `FakeCarrier` at
  whatever single place hands that carrier's adapters out. Then add its edge
  to `tests/integration/test_carrier_contract.py` (#201), which runs one
  `Script` per carrier and holds each to the same outcome.
- Outbound, limits, suppression, campaigns: `POST /v1/calls` and friends on
  `Harness.api`; assert on `carrier.actions_of("dial")` (an empty list is the
  suppression proof). A controllable clock is the campaign ticket's to add.
- Load (#200): in a separate server process, call `install_stub_providers(
  script)` and `FakeCarrier().install()` with the default `setattr`, then run
  the app — no production code path knows the stubs exist — and drive it
  with many `TwilioCall`s. Audio is paced in real time on both sides already.

Determinism notes: the caller speaks a 440 Hz tone, which Silero VAD scores
below 0.1, so turns start and end on the stub's finalized transcripts alone;
agent speech is the same tone, `Script.tts_ms_per_word` long per word.
"""

from tests.harness.app import HARNESS_AGENT, Harness, WebhookSink, run_app
from tests.harness.carrier import CarrierAction, FakeCarrier
from tests.harness.stubs import Script, ToolCall, install_stub_providers
from tests.harness.twilio import TwilioCall, place_inbound_call, post_signed, sign

__all__ = [
    "HARNESS_AGENT",
    "CarrierAction",
    "FakeCarrier",
    "Harness",
    "Script",
    "ToolCall",
    "TwilioCall",
    "WebhookSink",
    "install_stub_providers",
    "place_inbound_call",
    "post_signed",
    "run_app",
    "sign",
]
