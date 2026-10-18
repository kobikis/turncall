"""`call.ended` carries the call's last lines when the carrier says the call
is over before its pipeline does (#356).

The caller says a last line and hangs up: Twilio's `completed` status callback
can land while the pipeline is still storing that line. The callback used to
start post-call processing itself, which read the transcript straight away
and sent `call.ended` without it. Now, on a call a pipeline runs, the callback
only ends the call, and the pipeline starts post-call processing once it has
finished. Forced here by posting `completed` before the caller has spoken.
"""

import pytest

from tests.harness import (
    HARNESS_AGENT,
    Script,
    place_inbound_call,
    post_signed,
    run_app,
)

pytestmark = pytest.mark.integration

SCRIPT = Script(caller=("My last line.",), agent=("Noted, goodbye.",))
AGENT = {**HARNESS_AGENT, "first_message": "Hello from the harness."}


async def test_call_ended_waits_for_the_pipelines_last_lines(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await place_inbound_call(
            app.carrier, app.api, from_number="+15550001234", to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        # The carrier's `completed`, ahead of the pipeline's last lines.
        response = await post_signed(
            app.api,
            app.carrier,
            app.carrier.number(number).status_callback_url,
            call._params(CallStatus="completed", CallDuration="1"),
        )
        response.raise_for_status()
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = (await app.sink.wait_for("call.ended"))["payload"]
        await app.post_call_settled()
        sent = [e["event"] for e in app.sink.events if e["call_id"] == call.call_id]

    said = [(t["role"], t["text"]) for t in ended["transcript"]]
    assert ("customer", "My last line.") in said, said
    assert ("agent", "Noted, goodbye.") in said, said
    assert ended["status"] == "completed"
    assert sent.count("call.ended") == 1, sent
