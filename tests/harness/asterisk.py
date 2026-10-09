"""A fake Asterisk: ARI's REST + events WebSocket, and chan_websocket media.

What a real Asterisk does for TurnCall's ARI edge (#209), in order: TurnCall
connects to `/ari/events?app=...` (basic auth); a dialplan `Stasis()` call
arrives as `StasisStart`; TurnCall answers it and asks for an external-media
channel, which Asterisk dials out to TurnCall's media route
(websocket_client.conf's `uri`), opening with a JSON `MEDIA_START` and then
raw ulaw both ways, 160 bytes per 20ms; once that channel is up a `Dial`
event says `ANSWER` and TurnCall bridges the two. A channel leaving the app
is `StasisEnd` — hung up, continued in the dialplan, or its WebSocket closed.
`FakeAsterisk` does that much and no more.

Every ARI request is logged in `requests`; the ones that are a carrier's REST
actions are also recorded on the shared `FakeCarrier` in its vocabulary, so a
contract test asserts one way for every carrier: hanging up the caller's
channel is `hang_up`, continuing it in the transfer context is `transfer`
(target = the extension), `dtmf` is `send_dtmf`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from aiohttp import WSMsgType, web

from tests.harness.carrier import FakeCarrier
from tests.harness.media_call import SILENCE, HearsTheAgent, mulaw_tone

FRAME_S = 0.02


@dataclass
class AsteriskCall(HearsTheAgent):
    """One call into the Stasis app, from the caller's side."""

    asterisk: FakeAsterisk
    call_sid: str  # the caller's channel id: TurnCall's handle on the call
    from_number: str
    to_number: str
    call_id: str = ""  # TURNCALL_CALL_ID, set on the channel by the edge
    media_id: str = ""
    _ws: Any = None
    in_stasis: bool = True
    bridged: asyncio.Event = field(default_factory=asyncio.Event)
    _outgoing: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    _loud_at: list[float] = field(default_factory=list)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def say(self, ms: int = 400) -> None:
        for frame in mulaw_tone(ms):
            await self._outgoing.put(frame)

    async def hang_up(self) -> None:
        """The caller hangs up: their channel leaves the app."""
        if self.in_stasis:
            await self.asterisk.leave_stasis(self)
        await asyncio.wait_for(self.closed.wait(), 5)
        await self.finish()

    async def finish(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _send_media(self) -> None:
        start = time.monotonic()
        sent = 0
        while True:
            try:
                payload = self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                payload = SILENCE
            await self._ws.send(payload)
            sent += 1
            await asyncio.sleep(max(0.0, start + sent * FRAME_S - time.monotonic()))

    async def _receive(self) -> None:
        import audioop

        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    self.heard(audioop.ulaw2lin(message, 2))
                else:
                    self.asterisk.commands.append(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed.set()
            # Asterisk hangs up a WebSocket channel whose socket closed.
            await self.asterisk.media_gone(self)


class FakeAsterisk:
    """One Asterisk with one Stasis app, serving ARI on 127.0.0.1."""

    def __init__(self, carrier: FakeCarrier, media_url: str) -> None:
        self.carrier = carrier
        self.media_url = media_url  # websocket_client.conf's `uri`
        self.username = "ari-" + secrets.token_hex(4)
        self.password = secrets.token_hex(16)
        self.app = "turncall"
        self.url = ""
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.commands: list[str] = []  # text sent on media sockets
        self.connected = asyncio.Event()
        # False: external media is created but never dials TurnCall — a call
        # admitted whose media stream never comes.
        self.media_dials = True
        self._events: web.WebSocketResponse | None = None
        self._calls: dict[str, AsteriskCall] = {}  # by caller and media id
        self._runner: web.AppRunner | None = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/ari/events", self._events_ws)
        app.router.add_route("*", "/ari/{path:.*}", self._rest)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for call in list(self._calls.values()):
            await call.finish()
        if self._events is not None:
            await self._events.close()
        if self._runner is not None:
            await self._runner.cleanup()

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {token}":
            return web.json_response({"message": "Authentication required"}, status=401)
        return await handler(request)

    # -- the caller's side --------------------------------------------------

    async def ring(
        self, from_number: str, to_number: str, *, until_bridged: bool = True
    ) -> AsteriskCall:
        """A dialplan `Stasis(turncall)` on a call to `to_number`. Returns once
        TurnCall's media stream is up and bridged to the caller, or at once
        with `until_bridged=False` (a call TurnCall may refuse)."""
        if not self.connected.is_set():
            raise AssertionError("TurnCall's ARI edge never connected")
        call = AsteriskCall(
            asterisk=self,
            call_sid=f"{time.time():.6f}",  # what Asterisk's ids look like
            from_number=from_number,
            to_number=to_number,
        )
        self._calls[call.call_sid] = call
        await self._event(
            "StasisStart",
            channel=self._channel(call.call_sid, from_number, to_number),
            args=[],
        )
        if until_bridged:
            await asyncio.wait_for(call.bridged.wait(), 10)
        return call

    async def leave_stasis(self, call: AsteriskCall) -> None:
        call.in_stasis = False
        await self._event("StasisEnd", channel={"id": call.call_sid})
        if call._ws is None:
            call.closed.set()

    async def media_gone(self, call: AsteriskCall) -> None:
        if self._calls.pop(call.media_id, None) is not None:
            await self._event("StasisEnd", channel={"id": call.media_id})

    # -- ARI --------------------------------------------------------------

    async def _events_ws(self, request: web.Request) -> web.WebSocketResponse:
        if request.query.get("app") != self.app:
            raise web.HTTPBadRequest(text="unknown app")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._events = ws
        self.connected.set()
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
        return ws

    async def _event(self, kind: str, **body: Any) -> None:
        if self._events is None or self._events.closed:
            return  # nobody listening, as on a real Asterisk
        await self._events.send_str(
            json.dumps({"type": kind, "application": self.app, **body})
        )

    def _channel(self, channel_id: str, caller: str, exten: str) -> dict[str, Any]:
        return {
            "id": channel_id,
            "name": f"PJSIP/trunk-{channel_id}",
            "state": "Ring",
            "caller": {"name": "", "number": caller},
            # A PBX's DIDs usually arrive without the `+`.
            "dialplan": {"context": "from-trunk", "exten": exten.lstrip("+")},
        }

    async def _rest(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        query = dict(request.query)
        self.requests.append((request.method, path, query))
        parts = path.split("/")
        if parts[0] == "bridges":
            return await self._bridges(request.method, parts, query)
        if parts[0] != "channels":
            return web.json_response({"message": "not faked"}, status=404)
        if parts[1:] == ["externalMedia"]:
            return await self._external_media(query)
        call = self._calls.get(parts[1])
        if call is None:
            return web.json_response({"message": "Channel not found"}, status=404)
        action = parts[2] if len(parts) > 2 else request.method
        return await self._channel_action(call, parts[1], action, query)

    async def _channel_action(
        self, call: AsteriskCall, channel_id: str, action: str, query: dict[str, str]
    ) -> web.StreamResponse:
        is_caller = channel_id == call.call_sid
        if action == "variable" and query.get("variable") == "TURNCALL_CALL_ID":
            call.call_id = query.get("value", "")
        elif action == "DELETE" and is_caller:
            await self.carrier.record(
                "hang_up",
                self.username,
                call_sid=channel_id,
                reason=query.get("reason"),
            )
            await self.leave_stasis(call)
        elif action == "DELETE":
            with contextlib.suppress(Exception):
                await call._ws.close()
        elif action == "continue" and query.get("extension"):
            await self.carrier.record(
                "transfer",
                self.username,
                call_sid=channel_id,
                target_number=query["extension"],
                context=query.get("context"),
                transfer_message=None,
                whisper_url=None,
            )
            await self.leave_stasis(call)
        elif action == "continue":  # refused: back to the dialplan
            await self.carrier.record("continue", self.username, call_sid=channel_id)
            await self.leave_stasis(call)
        elif action == "dtmf":
            await self.carrier.record(
                "send_dtmf", self.username, call_sid=channel_id, digits=query["dtmf"]
            )
        return web.Response(status=204)

    async def _external_media(self, query: dict[str, str]) -> web.StreamResponse:
        media_id = query["channelId"]
        call = next(c for c in self._calls.values() if c.in_stasis and not c.media_id)
        call.media_id = media_id
        self._calls[media_id] = call
        await self._event(
            "StasisStart",
            channel={"id": media_id, "name": f"WebSocket/{query['external_host']}"},
            args=[],
        )
        if self.media_dials:
            call._tasks.append(asyncio.create_task(self._dial_media(call, query)))
        return web.json_response({"id": media_id})

    async def _dial_media(self, call: AsteriskCall, query: dict[str, str]) -> None:
        """chan_websocket's client side: connect, MEDIA_START, then media."""
        call._ws = await websockets.connect(self.media_url, subprotocols=["media"])
        await call._ws.send(
            json.dumps(
                {
                    "event": "MEDIA_START",
                    "connection_id": secrets.token_hex(8),
                    "channel": f"WebSocket/{query['external_host']}/0x1",
                    "channel_id": call.media_id,
                    "format": query["format"],
                    "optimal_frame_size": 160,
                    "ptime": 20,
                }
            )
        )
        call._tasks += [
            asyncio.create_task(call._send_media()),
            asyncio.create_task(call._receive()),
        ]
        await self._event(
            "Dial", peer={"id": call.media_id}, dialstatus="ANSWER", dialstring=""
        )

    async def _bridges(
        self, method: str, parts: list[str], query: dict[str, str]
    ) -> web.StreamResponse:
        if method == "POST" and parts[2:] == ["addChannel"]:
            channels = query["channel"].split(",")
            call = self._calls.get(channels[0])
            if call is None or set(channels) != {call.call_sid, call.media_id}:
                return web.json_response({"message": "not both legs"}, status=422)
            call.bridged.set()
        return web.Response(status=204)
