"""Asterisk's inbound edge: an ARI client, not webhook routes (#209, ADR-0022).

A dialplan line `Stasis(turncall)` (the app name is the account's) puts a
call into TurnCall's Stasis app; ARI announces it as `StasisStart` on the
events WebSocket TurnCall keeps open to every Asterisk it serves. The edge
then, per call:

1. resolves the dialled number (the first Stasis argument, else the channel's
   extension) to an Asterisk-carrier number and admits the call exactly as
   every carrier does (`services/inbound_call.py`). A call it cannot take is
   handed back to the dialplan at the priority after `Stasis()`, unanswered,
   so the PBX's own fallback (a queue, voicemail) gets it;
2. answers the caller and asks for an external-media channel — chan_websocket,
   which dials TurnCall's media route (`asterisk_media_stream.py`), and from
   there the shared media entry point runs the call;
3. bridges the caller and the media channel once Asterisk reports the media
   channel answered (a `Dial` event).

The two channels then live and die together, which ARI does not do for us:
when the media channel ends (the agent hung up, or the pipeline died) the
caller is hung up, and when the caller leaves Stasis (they hung up, or a cold
transfer continued them in the dialplan) the media channel is hung up — never
the reverse of a transfer, which would cut the caller off from the operator.

Each call's events are handled in order under its own lock, so a caller who
hangs up while the call is still being admitted is torn down after it, not
lost. The bookkeeping is in memory on the process holding the ARI connection;
the media stream may land on any process, since it carries the call id.

One process per Asterisk app: ARI delivers an app's events to the newest
WebSocket only, and the older one does not get them back when it leaves. With
several replicas, `ASTERISK_EDGE_ENABLED` stays on in exactly one. See
CLAUDE.md, "Carriers".
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import websockets
from loguru import logger

from turncall.domain.carriers import ASTERISK
from turncall.domain.enums import CallEventType, CallStatus
from turncall.webhooks.asterisk_media_stream import media_channel_id

if TYPE_CHECKING:
    from turncall.adapters.telephony.asterisk_adapter import AriAccount
    from turncall.services.inbound_call import Refused

CALL_ID_VARIABLE = "TURNCALL_CALL_ID"
_MAX_BACKOFF_S = 30.0


@dataclass
class _Call:
    """One call's two channels and the bridge between them. Registered the
    moment the caller enters Stasis, before admission knows its call id."""

    caller_id: str
    call_id: UUID | None = None
    project_id: UUID | None = None
    media_id: str = ""
    bridge_id: str = field(default_factory=lambda: str(uuid4()))
    caller_in_stasis: bool = True
    media_alive: bool = False
    media_answered: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _e164ish(number: str) -> str:
    """A PBX's digits as TurnCall stores numbers: `+` and digits."""
    number = number.strip()
    return f"+{number}" if number.isdigit() else number


class AriEdge:
    """One ARI connection: one Asterisk, the platform's or one project's."""

    def __init__(self, account: AriAccount, project_id: UUID | None) -> None:
        self.account = account
        self.project_id = project_id
        self._adapter = account.adapter()
        self._calls: dict[str, _Call] = {}  # by caller and by media channel id
        self._tasks: set[asyncio.Task[None]] = set()

    # -- the connection --------------------------------------------------

    async def run(self) -> None:
        """Keep the events WebSocket open, reconnecting with backoff."""
        token = f"{self.account.username}:{self.account.password}".encode()
        headers = {"Authorization": f"Basic {base64.b64encode(token).decode()}"}
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.account.events_url, additional_headers=headers
                ) as ws:
                    logger.info("asterisk_ari_connected", url=self.account.url)
                    backoff = 1.0
                    async for raw in ws:
                        self._dispatch(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "asterisk_ari_disconnected", url=self.account.url, error=str(exc)
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    def _dispatch(self, event: dict[str, Any]) -> None:
        logger.debug(
            "asterisk_event: {type} {channel} {status}",
            type=event.get("type"),
            channel=(event.get("channel") or event.get("peer") or {}).get("id"),
            status=event.get("dialstatus", ""),
        )
        handler = {
            "StasisStart": self._on_stasis_start,
            "Dial": self._on_dial,
            "StasisEnd": self._on_stasis_end,
            "ApplicationReplaced": self._on_replaced,
        }.get(event.get("type", ""))
        if handler is None:
            return
        task = asyncio.create_task(handler(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- events ----------------------------------------------------------

    async def _on_stasis_start(self, event: dict[str, Any]) -> None:
        channel = event.get("channel", {})
        caller_id = channel.get("id", "")
        if not caller_id or caller_id in self._calls:
            return  # our own media channel entering the app
        call = self._calls[caller_id] = _Call(caller_id)
        # Taken before the first await: this call's later events queue behind.
        async with call.lock:
            try:
                await self._admit(call, channel, event.get("args") or [])
            except Exception:
                logger.exception("asterisk_admit_failed", channel=caller_id)
                call.media_alive = False
                if call.call_id is None:  # no record to end; nothing to track
                    self._calls.pop(caller_id, None)
                with contextlib.suppress(Exception):
                    await self._adapter.continue_in_dialplan(caller_id)

    async def _admit(
        self, call: _Call, channel: dict[str, Any], args: list[str]
    ) -> None:
        from turncall.services.inbound_call import Refused

        dialled = _e164ish(args[0] if args else channel["dialplan"]["exten"])
        caller = _e164ish(channel.get("caller", {}).get("number", "")) or None
        admitted = await self._admit_call(call.caller_id, dialled, caller)
        if isinstance(admitted, Refused):
            logger.warning(
                "asterisk_call_refused", to_number=dialled, reason=admitted.message
            )
            self._calls.pop(call.caller_id, None)
            await self._adapter.continue_in_dialplan(call.caller_id)
            return
        call.call_id, call.project_id = admitted
        call.media_id = media_channel_id(call.call_id)
        self._calls[call.media_id] = call
        await self._adapter.set_variable(
            call.caller_id, CALL_ID_VARIABLE, str(call.call_id)
        )
        await self._adapter.answer(call.caller_id)
        call.media_alive = True
        await self._adapter.external_media(call.media_id)
        logger.info("asterisk_inbound_connected", call_id=str(call.call_id), to=dialled)

    async def _admit_call(
        self, caller_id: str, dialled: str, caller: str | None
    ) -> tuple[UUID, UUID] | Refused:
        """The call's id and project, or why this Asterisk cannot take it."""
        from turncall.services.inbound_call import Refused, admit_inbound_call
        from turncall.storage.database import get_session_factory
        from turncall.storage.repositories import credential_repo, phone_number_repo

        async with get_session_factory()() as session:
            number = await phone_number_repo.get_by_e164(session, dialled)
            if number is None or number.provider != ASTERISK:
                return Refused("This number is not configured.")
            if self.project_id is not None and number.project_id != self.project_id:
                return Refused("This number belongs to another project's Asterisk.")
            if self.project_id is None and ASTERISK in (
                await credential_repo.providers_for_project(session, number.project_id)
            ):
                return Refused("This number's project rides its own Asterisk.")
            call = await admit_inbound_call(
                session,
                number,
                from_number=caller or "",
                to_number=dialled,
                call_sid=caller_id,
                carrier=ASTERISK,
            )
            if isinstance(call, Refused):
                return call
            await session.commit()
            return call.id, call.project_id

    async def _on_replaced(self, _event: dict[str, Any]) -> None:
        """ARI gives an app to one WebSocket at a time: the newest. This
        connection stays open but gets no more calls, and does not get them
        back when the newer one goes away — measured on Asterisk 23."""
        logger.warning(
            "asterisk_ari_app_replaced: another connection took app {app} on "
            "{url}; this process takes no more calls from it",
            app=self.account.app,
            url=self.account.url,
        )

    async def _on_dial(self, event: dict[str, Any]) -> None:
        peer = event.get("peer", {}).get("id", "")
        call = self._calls.get(peer)
        if call is None or peer != call.media_id or event.get("dialstatus") != "ANSWER":
            return
        async with call.lock:
            if call.media_answered or not (call.caller_in_stasis and call.media_alive):
                return
            call.media_answered = True
            await self._adapter.bridge(call.bridge_id, [call.caller_id, call.media_id])

    async def _on_stasis_end(self, event: dict[str, Any]) -> None:
        channel_id = event.get("channel", {}).get("id", "")
        call = self._calls.get(channel_id)
        if call is None:
            return
        async with call.lock:
            if call.call_id is None:  # refused: handed back to the dialplan
                return
            if channel_id == call.caller_id:
                call.caller_in_stasis = False
                if call.media_alive:
                    # Its own StasisEnd then finds nothing left to do.
                    call.media_alive = False
                    await self._quietly(self._adapter.end_call(call.media_id))
                if not call.media_answered:
                    await self._abandoned(call)
            else:
                call.media_alive = False
                if call.caller_in_stasis:
                    await self._quietly(self._adapter.end_call(call.caller_id))
            if not (call.caller_in_stasis or call.media_alive):
                if call.media_answered:
                    await self._quietly(self._adapter.destroy_bridge(call.bridge_id))
                self._calls.pop(call.caller_id, None)
                self._calls.pop(call.media_id, None)

    async def _abandoned(self, call: _Call) -> None:
        """The caller left before the media stream was up: no pipeline ran, so
        nothing else will end the call record."""
        from turncall.events.emit import emit_call_event
        from turncall.storage.database import get_session_factory
        from turncall.storage.repositories import call_repo

        if call.call_id is None or call.project_id is None:
            return  # never admitted: there is no call record
        async with get_session_factory()() as session:
            row = await call_repo.get_call_by_id(session, call.call_id)
            # Past INITIATED, the media stream got there first and the
            # pipeline ends the call itself.
            if row is None or row.status != CallStatus.INITIATED.value:
                return
            await call_repo.update_call_status(
                session, call.call_id, status=CallStatus.FAILED.value
            )
            await emit_call_event(
                session,
                call_id=call.call_id,
                project_id=call.project_id,
                event_type=CallEventType.CALL_FAILED,
                payload={"reason": "caller left before the media stream started"},
            )
            await session.commit()

    @staticmethod
    async def _quietly(request: Any) -> None:
        """A teardown request: the channel or bridge may already be gone."""
        try:
            await request
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                logger.warning("asterisk_teardown_failed", error=str(exc))
        except Exception as exc:
            logger.warning("asterisk_teardown_failed", error=str(exc))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class AsteriskEdges:
    """One `AriEdge` per Asterisk this deployment serves (ADR-0020): the
    platform's, and each project's own. Reconciled at start, every minute
    (another process may have written a credential), and at once after a
    credential write on this one (`poke`)."""

    def __init__(self) -> None:
        self._running: dict[UUID | None, tuple[AriAccount, asyncio.Task[None]]] = {}
        self._wake: asyncio.Event | None = None

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run(self, interval_s: float = 60.0) -> None:
        # Made here, in the loop that runs it: an app is started once per
        # event loop, and a test suite starts many.
        self._wake = wake = asyncio.Event()
        try:
            while True:
                wake.clear()
                try:
                    await self.reconcile()
                except Exception:
                    logger.exception("asterisk_edges_reconcile_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), interval_s)
        finally:
            for _, task in self._running.values():
                task.cancel()
            await asyncio.gather(
                *(t for _, t in self._running.values()), return_exceptions=True
            )
            self._running.clear()
            self._wake = None

    async def reconcile(self) -> None:
        from turncall.config.settings import get_settings
        from turncall.services.asterisk_accounts import all_accounts
        from turncall.storage.database import get_session_factory

        async with get_session_factory()() as session:
            wanted = await all_accounts(session, get_settings())
        for key, (account, task) in list(self._running.items()):
            if wanted.get(key) != account or task.done():
                task.cancel()
                del self._running[key]
        for key, account in wanted.items():
            if key not in self._running:
                edge = AriEdge(account, key)
                self._running[key] = (account, asyncio.create_task(_run(edge)))


async def _run(edge: AriEdge) -> None:
    try:
        await edge.run()
    finally:
        await edge.close()


EDGES = AsteriskEdges()
