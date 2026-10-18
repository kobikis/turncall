"""The load generator's own arithmetic (#200): what counts as an underrun,
and which step a pod's capacity is read from.

The figure in docs/guides/capacity.mdx rests on these two judgements, so they
are pinned here; the measurement itself needs a pod and runs by hand
(`make load-test`).
"""

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

# The real package, before the client loads: without pytest the client puts an
# empty stand-in for `tests.harness` in sys.modules if none is there, and a
# later `from tests.harness import ...` in another test module would find it
# (#346).
import tests.harness  # noqa: F401

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[2] / "scripts/calls_per_pod/client.py"
_spec = importlib.util.spec_from_file_location("calls_per_pod_client", _PATH)
client = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = client  # @dataclass resolves its module by name
_spec.loader.exec_module(client)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(
        client, "time", SimpleNamespace(monotonic=fake.monotonic, time=fake.time)
    )
    return fake


def _call():
    return client.LoadCall(
        carrier=None,
        http=None,
        call_sid="",
        from_number="",
        to_number="",
        call_id="",
        stream_sid="",
        twiml="",
        _ws=None,
    )


def _play(call, clock, arrivals, chunk=0.04):
    """Deliver one `chunk`-long frame at each offset (seconds from now)."""
    start = clock.now
    for offset in arrivals:
        clock.now = start + offset
        call._heard(chunk)


def test_audio_paced_in_real_time_plays_without_an_underrun(clock):
    call = _call()
    _play(call, clock, [i * 0.04 for i in range(50)])
    assert call.underruns == []
    assert len(call.utterance_starts) == 1


def test_a_frame_later_than_the_playout_buffer_is_an_underrun(clock):
    call = _call()
    # Frame 5 arrives 100ms late; the 60ms buffer covers only part of it.
    arrivals = [i * 0.04 for i in range(5)] + [
        5 * 0.04 + 0.1 + i * 0.04 for i in range(5)
    ]
    _play(call, clock, arrivals)
    assert len(call.underruns) == 1
    assert call.underruns[0][1] == pytest.approx(0.1 - client.JITTER_S)


def test_lateness_inside_the_buffer_is_not_heard(clock):
    call = _call()
    arrivals = [i * 0.04 for i in range(5)] + [5 * 0.04 + 0.05]
    _play(call, clock, arrivals)
    assert call.underruns == []


def test_a_pause_after_the_audio_ran_out_starts_a_new_utterance(clock):
    call = _call()
    _play(call, clock, [0.0, 0.04, 2.0, 2.04])
    assert call.underruns == []
    assert len(call.utterance_starts) == 2
    assert call.reply_after(clock.now - 0.1) == pytest.approx(clock.now - 0.04)


def _step(calls, *, p95=400, underruns=0.0, unanswered=0, lag=2.0):
    return {
        "calls": calls,
        "connected": calls,
        "not_greeted": 0,
        "dropped": 0,
        "unanswered": unanswered,
        "underruns_per_audio_min": underruns,
        "turn_latency_ms": {"p50": 350, "p95": p95},
        "client_lag_p99_ms": lag,
    }


ARGS = Namespace(max_underruns_per_min=1.0, max_latency_rise_ms=300.0)


def test_capacity_is_the_last_step_before_the_first_degraded_one():
    steps = client.verdict(
        [_step(1), _step(4), _step(8, underruns=3.0), _step(12)], ARGS
    )
    assert [s["ok"] for s in steps] == [True, True, False, True]
    assert client.capacity(steps) == 4


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        (_step(4, p95=700), "p95 latency"),
        (_step(4, unanswered=1), "unanswered"),
        (_step(4, lag=50.0), "client-bound"),
    ],
)
def test_each_degradation_is_named(step, reason):
    steps = client.verdict([_step(1), step], ARGS)
    assert not steps[1]["ok"]
    assert any(reason in why for why in steps[1]["why_not"])
