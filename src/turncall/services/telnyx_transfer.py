"""A transfer on a Telnyx call: ADR-0009 on Call Control (ADR-0022).

Twilio does a transfer in one TwiML document: `<Say>` the transfer message,
then `<Dial>` the operator while the caller hears ringing. Call Control has no
document. Each of those is a command on one leg, and the next step starts when
Telnyx reports the last one done. So a transfer here is a short conversation
with Telnyx's webhooks:

1. `start` (from call control) stops the agent's media stream, then speaks the
   transfer message on the caller's leg if there is one (its
   `call.speak.ended` goes on to step 2), else goes straight to step 2.
2. Ring the operator: ringback to the caller, and the operator dialled on a
   new leg linked to the call.
3. The operator's `call.answered` bridges the two legs. The operator's
   `call.hangup` before that means nobody answered, and the call ends.
4. If the caller hangs up while the operator still rings, the operator's leg
   is hung up too.

Each command carries a `client_state` naming the TurnCall call and the step,
which Telnyx echoes on every later webhook of that leg. That is how the
operator's leg, which has no call row, is traced back to its call, and how
step 1's `call.speak.ended` is told apart from any other.

The intent (target, message, ...) is the one call control parked in Redis
(`services/transfer.py`); the operator's leg is added to it once dialled.
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import wave
from dataclasses import replace
from functools import cache
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.services import transfer as transfer_svc
from turncall.services.transfer import TransferIntent

# Where the inbound edge keeps the call's Call Control application id, which a
# new leg has to be dialled on (calls.metadata_json).
CONNECTION_KEY = "telnyx_connection_id"

_STATE_KEY = "transfer"
STARTED = "started"  # caller's leg: the agent's stream stopped
MESSAGE = "message"  # caller's leg: the transfer message is playing
OPERATOR = "operator"  # operator's leg: ringing
BRIDGED = "bridged"  # operator's leg: connected to the caller


class TransferUnavailable(RuntimeError):
    """This call cannot be transferred on Telnyx."""


def client_state(call_id: UUID, step: str, **extra: Any) -> str:
    """The `client_state` a transfer command carries (base64 JSON, as Telnyx
    requires)."""
    state = {_STATE_KEY: str(call_id), "step": step, **extra}
    return base64.b64encode(json.dumps(state).encode()).decode()


def read_client_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A transfer's `client_state` on a webhook, or None if it carries none.

    Unsigned until the webhook's signature is checked: before that it may only
    pick which project's key does the checking.
    """
    raw = payload.get("client_state")
    if not raw:
        return None
    try:
        state = json.loads(base64.b64decode(raw))
        UUID(str(state[_STATE_KEY]))
    except (ValueError, TypeError, KeyError):
        return None
    return state if isinstance(state, dict) else None


def call_id_of(state: dict[str, Any]) -> UUID:
    return UUID(str(state[_STATE_KEY]))


@cache
def ringback_wav() -> bytes:
    """What the caller hears while the operator's phone rings: North American
    ringback, 440 + 480 Hz for two seconds then four silent, one cycle (Telnyx
    loops it). Call Control rings nobody on a leg it dialled itself, where
    Twilio's `<Dial>` passes ringback through."""
    rate, on, cycle = 8000, 2, 6
    samples = (
        int(
            0.2
            * 32767
            * (
                math.sin(2 * math.pi * 440 * n / rate)
                + math.sin(2 * math.pi * 480 * n / rate)
            )
        )
        if n < on * rate
        else 0
        for n in range(cycle * rate)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buffer.getvalue()


async def start(
    adapter: Any, call: Any, intent: TransferIntent, *, detect_machine: bool
) -> None:
    """Step 1. Raises when the transfer could not be started; if the agent's
    stream was already stopped by then, the caller is hung up rather than left
    on a silent line."""
    if not (call.metadata_json or {}).get(CONNECTION_KEY):
        raise TransferUnavailable("no Call Control application recorded for this call")
    caller = call.provider_call_sid
    # A state from the first command on, so every later webhook of the
    # caller's leg (its hangup included) is known to belong to a transfer.
    await adapter.stop_streaming(caller, client_state=client_state(call.id, STARTED))
    try:
        if intent.transfer_message:
            await adapter.speak(
                caller,
                intent.transfer_message,
                client_state=client_state(call.id, MESSAGE, amd=detect_machine),
            )
        else:
            await _ring_operator(adapter, call, intent, detect_machine=detect_machine)
    except Exception:
        await _hang_up(adapter, caller)
        raise


async def on_event(
    session: AsyncSession,
    adapter: Any,
    call: Any,
    event_type: str,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """One verified webhook of a transfer, on either leg. Never raises: a
    webhook Telnyx sees fail is retried, and a retried step is a second dial."""
    leg = str(payload.get("call_control_id") or "")
    try:
        if leg == call.provider_call_sid:
            await _on_caller_event(adapter, call, event_type, payload, state)
        else:
            await _on_operator_event(session, adapter, call, leg, event_type, state)
    except Exception:
        logger.exception(
            "telnyx_transfer_step_failed", call_id=str(call.id), event_type=event_type
        )


async def _on_caller_event(
    adapter: Any,
    call: Any,
    event_type: str,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> None:
    caller = call.provider_call_sid
    if event_type == "call.hangup":
        # The caller gave up while the operator was still ringing: stop that
        # (after a bridge Telnyx has hung the other leg up already).
        intent = await transfer_svc.load_transfer_intent(call.id)
        if intent is not None and intent.operator_leg:
            await _hang_up(adapter, intent.operator_leg)
        return
    finished = event_type == "call.speak.ended" and payload.get("status") == "completed"
    if not finished or state.get("step") != MESSAGE:
        return
    intent = await transfer_svc.load_transfer_intent(call.id)
    if intent is None:
        logger.error("telnyx_transfer_intent_lost", call_id=str(call.id))
        await _hang_up(adapter, caller)
        return
    try:
        await _ring_operator(
            adapter, call, intent, detect_machine=bool(state.get("amd"))
        )
    except Exception:
        logger.exception("telnyx_transfer_dial_failed", call_id=str(call.id))
        await _hang_up(adapter, caller)


async def _on_operator_event(
    session: AsyncSession,
    adapter: Any,
    call: Any,
    leg: str,
    event_type: str,
    state: dict[str, Any],
) -> None:
    if event_type == "call.answered" and state.get("step") == OPERATOR:
        await _bridge(adapter, call, leg)
    elif event_type == "call.hangup" and state.get("step") != BRIDGED:
        # Busy, no answer, rejected — or gone before the bridge: nobody took
        # the caller, so the call ends (ADR-0009: no return to the agent).
        logger.info("telnyx_transfer_not_answered", call_id=str(call.id))
        await _stop_ringback(adapter, call.provider_call_sid)
        await _hang_up(adapter, call.provider_call_sid)


async def _ring_operator(
    adapter: Any, call: Any, intent: TransferIntent, *, detect_machine: bool
) -> None:
    """Step 2: ringback to the caller, the operator dialled on a linked leg."""
    caller = call.provider_call_sid
    await adapter.start_playback(caller, ringback_wav())
    leg = await adapter.dial(
        connection_id=call.metadata_json[CONNECTION_KEY],
        to=intent.target_number,
        # Replaced by the caller's own number under `bridge_intent`.
        from_number=call.from_number or call.to_number or "",
        link_to=caller,
        client_state=client_state(call.id, OPERATOR),
        detect_machine=detect_machine,
    )
    await transfer_svc.store_transfer_intent(call.id, replace(intent, operator_leg=leg))


async def _bridge(adapter: Any, call: Any, operator: str) -> None:
    """Step 3: the caller and the operator, connected."""
    caller = call.provider_call_sid
    await _stop_ringback(adapter, caller)
    try:
        await adapter.bridge(
            operator, caller, client_state=client_state(call.id, BRIDGED)
        )
    except Exception:
        # The caller hung up first: do not leave the operator on a dead line.
        logger.exception("telnyx_transfer_bridge_failed", call_id=str(call.id))
        await _hang_up(adapter, operator)


async def _stop_ringback(adapter: Any, caller: str) -> None:
    try:
        await adapter.stop_playback(caller)
    except Exception:
        logger.warning("telnyx_transfer_ringback_not_stopped", call_control_id=caller)


async def _hang_up(adapter: Any, call_control_id: str) -> None:
    """Best-effort: an already-ended call counts as hung up (the adapter's)."""
    try:
        await adapter.end_call(call_control_id)
    except Exception:
        logger.exception(
            "telnyx_transfer_hangup_failed", call_control_id=call_control_id
        )
