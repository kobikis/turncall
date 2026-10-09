"""A request's writes are committed before its response is sent.

TwiML answering an inbound call names the call by id, and Twilio opens the
media stream the moment it reads it; the stream then reads the call row. FastAPI
runs a yield dependency's exit code *after* the response is sent unless it is
scoped to the function, so `get_session`'s commit used to land after the TwiML
had gone out — a carrier quicker than the commit got `media_stream_call_not_found`
and a caller heard silence (seen in CI). A slow commit makes that race certain
— first for binding the number (the voice webhook found no number), then for
the call row itself.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.harness import HARNESS_AGENT, Script, place_inbound_call, run_app

pytestmark = pytest.mark.integration

COMMIT_DELAY_SECONDS = 0.5


async def test_the_call_row_exists_when_the_twiml_reaches_the_carrier(monkeypatch):
    commit = AsyncSession.commit

    async def slow_commit(self: AsyncSession) -> None:
        await asyncio.sleep(COMMIT_DELAY_SECONDS)
        await commit(self)

    async with run_app(
        Script(caller=(), agent=()),
        set_env=monkeypatch.setenv,
        set_attr=monkeypatch.setattr,
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {**HARNESS_AGENT, "first_message": "Hello from the harness."}
            )
        )
        monkeypatch.setattr(AsyncSession, "commit", slow_commit)

        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting: the stream found its row
        await call.finish()

    assert call.agent_utterances() >= 1
