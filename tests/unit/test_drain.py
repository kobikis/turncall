"""The drain behind graceful shutdown (#199), short of a real process.

`tests/integration/test_graceful_shutdown.py` signals a real server; these pin
the pieces that are slow or impossible to reach there — the bounds (an hour is
not a test), a second SIGTERM, what the probes answer, and that the
fire-and-forget transports (WebRTC, WhatsApp voice) count their calls too.
"""

import asyncio
import os
import signal
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.models import AgentConfig
from turncall.services import drain

pytestmark = pytest.mark.unit

POST_CALL_WORK = drain._post_call_work  # the fixture stubs it out


@pytest.fixture(autouse=True)
def _fresh_drain(monkeypatch):
    monkeypatch.setattr(drain, "_live", {})
    monkeypatch.setattr(drain, "_draining", False)
    monkeypatch.setattr(drain, "POLL_SECONDS", 0.01)
    # Other tests' leftover webhook tasks are not this test's post-call work.
    monkeypatch.setattr(drain, "_post_call_work", list)
    original = signal.getsignal(signal.SIGTERM)
    yield
    drain.uninstall_sigterm_drain()
    signal.signal(signal.SIGTERM, original)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 10.0  # each look at the clock costs ten seconds
        return self.now


def test_a_live_call_is_counted_until_its_block_exits_even_by_raising():
    with pytest.raises(RuntimeError):
        with drain.live_call(uuid4(), 600):
            assert drain.active_calls() == 1
            raise RuntimeError("the call failed")
    assert drain.active_calls() == 0


async def test_drain_waits_for_the_live_call_then_its_post_call_work():
    post_call = asyncio.get_running_loop().create_future()

    async def call() -> None:
        with drain.live_call(uuid4(), 600):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        post_call.set_result(None)

    running = asyncio.create_task(call())
    await asyncio.sleep(0)  # the call is live
    await drain.drain(pending_work=lambda: [] if post_call.done() else [post_call])
    assert drain.active_calls() == 0
    assert post_call.done()
    await running


async def test_drain_gives_up_on_a_call_its_duration_guard_should_have_ended(
    monkeypatch,
):
    clock = Clock()
    monkeypatch.setattr(drain, "_live", {uuid4(): 120.0, uuid4(): 300.0})
    await drain.drain(pending_work=list, clock=clock)
    # Bounded by the longest call in play, not the first one.
    assert 300 <= clock.now < 330


async def test_drain_abandons_post_call_work_after_its_grace():
    clock = Clock()
    stuck = asyncio.get_running_loop().create_future()
    await drain.drain(pending_work=lambda: [stuck], clock=clock)
    assert (
        drain.POST_CALL_GRACE_SECONDS <= clock.now < drain.POST_CALL_GRACE_SECONDS + 30
    )
    stuck.cancel()


def test_a_call_without_a_budget_is_bounded_by_the_default():
    with drain.live_call(uuid4(), None):
        (ends_by,) = drain._live.values()
        remaining = ends_by - time.monotonic()
    budget = drain.DEFAULT_MAX_CALL_DURATION_SECONDS + drain.GUARD_SLACK_SECONDS
    assert budget - 5 < remaining <= budget


async def _until(condition, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)


async def _sigterm() -> None:
    """Signal this process, and give the loop time to act on it."""
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.2)


async def test_sigterm_drains_then_hands_the_signal_back():
    handed_back: list[int] = []
    signal.signal(signal.SIGTERM, lambda sig, _frame: handed_back.append(sig))
    drain.install_sigterm_drain()

    with drain.live_call(uuid4(), 600):
        await _sigterm()
        assert drain.is_draining()
        assert handed_back == [], "handed back while a call was live"
    await _until(lambda: handed_back)
    assert handed_back == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGTERM) is not drain._handler


async def test_a_second_sigterm_stops_waiting():
    handed_back: list[int] = []
    signal.signal(signal.SIGTERM, lambda sig, _frame: handed_back.append(sig))
    drain.install_sigterm_drain()

    with drain.live_call(uuid4(), 600):
        await _sigterm()
        assert handed_back == []
        await _sigterm()
        assert handed_back == [signal.SIGTERM]
    # The previous owner has SIGTERM back; the drain finishing later does not
    # deliver it a second time.
    await asyncio.sleep(0.1)
    assert handed_back == [signal.SIGTERM]


def test_post_call_work_is_what_sends_call_ended():
    from turncall.events import dispatcher
    from turncall.services import call_analysis_trigger

    post_call, delivery = object(), object()
    with (
        patch.object(call_analysis_trigger, "_POST_CALL_TASKS", {post_call}),
        patch.object(dispatcher, "_BG_TASKS", {delivery}),
    ):
        assert set(POST_CALL_WORK()) == {post_call, delivery}


def test_readiness_fails_while_liveness_and_the_gauge_answer(app, monkeypatch):
    with drain.live_call(uuid4(), 600):
        monkeypatch.setattr(drain, "_draining", True)
        ready = app.get("/ready")
        live = app.get("/live")
        metrics = app.get("/metrics")

    assert ready.status_code == 503
    assert ready.json() == {"status": "draining", "active_calls": 1}
    assert live.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert "# TYPE turncall_active_calls gauge" in metrics.text
    assert "turncall_active_calls 1\n" in metrics.text


async def test_webrtc_and_whatsapp_calls_are_counted_while_they_run():
    """start_call_pipeline is their shared entry point (media_stream is
    Twilio's, and the integration test covers it through the gauge)."""
    from types import SimpleNamespace

    from turncall.orchestrator import pipeline_builder
    from turncall.orchestrator.pipeline_factory import CallContext

    release = asyncio.Event()
    counted: list[int] = []

    class Session:
        async def start(self) -> None:
            counted.append(drain.active_calls())
            await release.wait()

    context = CallContext(
        call_id=uuid4(),
        project_id=uuid4(),
        agent_id=uuid4(),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory="SF",
    )
    with patch.object(
        pipeline_builder, "build_call_pipeline", new=AsyncMock(return_value=Session())
    ):
        await pipeline_builder.start_call_pipeline(
            config=AgentConfig(),
            transport="T",
            call_context=context,
            settings=SimpleNamespace(),
            session_factory="SF",
        )
        for _ in range(50):
            if counted:
                break
            await asyncio.sleep(0.01)
        assert counted == [1]
        release.set()
        for _ in range(50):
            if drain.active_calls() == 0:
                break
            await asyncio.sleep(0.01)
    assert drain.active_calls() == 0


def test_every_voice_entry_point_counts_its_calls():
    """A call nobody counted is one a SIGTERM does not wait for — dropped on the
    next deploy with nothing to say why. `start_call_pipeline` counts its own
    (WebRTC, WhatsApp voice); a module building a pipeline directly must too.
    The eval worker is exempt: an eval is not a call."""
    from pathlib import Path

    import turncall

    root = Path(turncall.__file__).parent
    builders = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "build_call_pipeline(" in path.read_text()
        and path.relative_to(root).parts[0] != "evals"
    }
    assert builders == {"orchestrator/pipeline_builder.py", "webhooks/media_stream.py"}
    for module in builders:
        assert "drain.live_call(" in (root / module).read_text(), module
