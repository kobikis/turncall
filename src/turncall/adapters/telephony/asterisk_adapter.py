"""Asterisk over ARI: its REST actions (#209, ADR-0022).

ARI is Asterisk's REST interface plus an events WebSocket. A call is a
*channel*; TurnCall's handle on it (`provider_call_sid`) is the caller's
channel id. Hang-up, cold transfer and DTMF are ARI requests on that channel.
The inbound side — the events WebSocket, the bridge and the external-media
channel that carries the audio — is the edge's, in `webhooks/asterisk_edge.py`,
which uses the extra ARI requests below.

What ARI cannot do the way TurnCall defines it raises
`UnsupportedCarrierAction` instead of doing something else under the same
name: there is no outbound dialling, no recording URL, no SMS and nothing to
configure on a number (the PBX's dialplan routes to TurnCall), and a transfer
is cold only — see CLAUDE.md, "Carriers".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from turncall.adapters.http_client import get_http_client
from turncall.adapters.telephony.base import TelephonyAdapter, UnsupportedCarrierAction

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass(frozen=True)
class AriAccount:
    """One Asterisk reached over ARI, and how TurnCall uses it.

    `media_connection` names the websocket_client.conf entry Asterisk dials
    TurnCall's media route through; `transfer_context` is the dialplan context
    a cold transfer continues the caller in, with the target as extension.
    """

    url: str
    username: str
    password: str
    app: str = "turncall"
    media_connection: str = "turncall"
    transfer_context: str = "turncall-transfer"

    def adapter(self) -> AsteriskAdapter:
        return AsteriskAdapter(self)

    @property
    def events_url(self) -> str:
        """The ARI events WebSocket for this account's Stasis app."""
        base = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/ari/events?app={self.app}"


class AsteriskAdapter(TelephonyAdapter):
    """ARI requests on one Asterisk."""

    def __init__(self, account: AriAccount) -> None:
        self._account = account

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """One ARI request; raises httpx.HTTPStatusError on a 4xx/5xx."""
        response = await get_http_client().request(
            method,
            f"{self._account.url}/ari/{path}",
            params=params,
            json=json,
            auth=(self._account.username, self._account.password),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return response.json() if response.content else None

    # -- TelephonyAdapter ------------------------------------------------

    async def end_call(self, provider_call_sid: str) -> None:
        await self.request("DELETE", f"channels/{provider_call_sid}")

    async def transfer_call(
        self,
        provider_call_sid: str,
        target_number: str,
        *,
        transfer_message: str | None = None,
        whisper_url: str | None = None,
        action_url: str | None = None,
        amd_callback_url: str | None = None,
    ) -> str:
        """A cold transfer: the caller leaves TurnCall's Stasis app and
        continues in the transfer context's dialplan, extension = target.

        `amd_callback_url` is ignored: transfer AMD only ever notifies
        (ADR-0009), and Asterisk has none here to notify about.
        """
        if whisper_url:
            raise UnsupportedCarrierAction(
                "A warm transfer is not available on Asterisk numbers; "
                "use a cold transfer."
            )
        if transfer_message or action_url:
            raise UnsupportedCarrierAction(
                "On Asterisk numbers a transfer cannot carry a transfer_message "
                "or a fallback_message; say it before transferring."
            )
        await self.request(
            "POST",
            f"channels/{provider_call_sid}/continue",
            params={
                "context": self._account.transfer_context,
                "extension": target_number,
                "priority": 1,
            },
        )
        return provider_call_sid

    async def send_dtmf(self, provider_call_sid: str, digits: str) -> None:
        await self.request(
            "POST", f"channels/{provider_call_sid}/dtmf", params={"dtmf": digits}
        )

    async def initiate_outbound_call(
        self,
        from_number: str,
        to_number: str,
        webhook_url: str,
        *,
        status_callback_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        raise UnsupportedCarrierAction("Outbound calls on Asterisk are not supported")

    async def get_recording_url(self, recording_sid: str) -> str:
        raise UnsupportedCarrierAction("Asterisk has no carrier-side recording")

    async def configure_number_webhook(
        self, number_sid: str, voice_url: str, status_callback_url: str
    ) -> None:
        raise UnsupportedCarrierAction("An Asterisk number is routed by its dialplan")

    async def fetch_number_info(self, number_sid: str) -> dict[str, Any]:
        raise UnsupportedCarrierAction("Asterisk keeps no number records")

    async def send_sms(self, from_number: str, to_number: str, body: str) -> str:
        raise UnsupportedCarrierAction("SMS is not available on Asterisk numbers")

    async def configure_sms_webhook(self, number_sid: str, sms_url: str) -> None:
        raise UnsupportedCarrierAction("SMS is not available on Asterisk numbers")

    # -- the inbound edge's requests ------------------------------------

    async def answer(self, channel_id: str) -> None:
        await self.request("POST", f"channels/{channel_id}/answer")

    async def set_variable(self, channel_id: str, name: str, value: str) -> None:
        await self.request(
            "POST",
            f"channels/{channel_id}/variable",
            params={"variable": name, "value": value},
        )

    async def continue_in_dialplan(self, channel_id: str) -> None:
        """Hand a channel back to the dialplan after its Stasis() call."""
        await self.request("POST", f"channels/{channel_id}/continue")

    async def external_media(self, channel_id: str) -> None:
        """A chan_websocket channel: Asterisk dials TurnCall's media route
        through the account's websocket_client.conf entry. ulaw at 8 kHz;
        JSON control messages, so MEDIA_START names the channel id."""
        await self.request(
            "POST",
            "channels/externalMedia",
            params={
                "channelId": channel_id,
                "app": self._account.app,
                "external_host": self._account.media_connection,
                "transport": "websocket",
                "encapsulation": "none",
                "connection_type": "client",
                "format": "ulaw",
                "transport_data": "f(json)",
            },
        )

    async def bridge(self, bridge_id: str, channel_ids: list[str]) -> None:
        await self.request("POST", f"bridges/{bridge_id}", params={"type": "mixing"})
        await self.request(
            "POST",
            f"bridges/{bridge_id}/addChannel",
            params={"channel": ",".join(channel_ids)},
        )

    async def destroy_bridge(self, bridge_id: str) -> None:
        await self.request("DELETE", f"bridges/{bridge_id}")
