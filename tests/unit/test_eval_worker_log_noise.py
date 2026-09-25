"""The readiness probe is not a failure, and must not read like one.

Pipecat's eval client checks the bot is listening by opening a TCP connection
and closing it without sending anything — on purpose, since a real handshake
would make the bot greet a connection about to be discarded. The `websockets`
server logs that as `opening handshake failed` with a traceback, once per
iteration, and it is indistinguishable in the log from a bot that genuinely
could not be reached.
"""

import logging

import pytest

from turncall.evals.worker import _DropReadinessProbe

pytestmark = pytest.mark.unit


def _record(exc: BaseException | None) -> logging.LogRecord:
    record = logging.LogRecord(
        "websockets.server",
        logging.ERROR,
        __file__,
        1,
        "opening handshake failed",
        (),
        None,
    )
    if exc is not None:
        record.exc_info = (type(exc), exc, None)
    return record


def _probe_exception() -> BaseException:
    """What websockets raises when a peer closes before sending a request."""
    cause = EOFError("stream ends after 0 bytes, before end of line")
    middle = EOFError("connection closed while reading HTTP request line")
    middle.__cause__ = cause
    top = RuntimeError("did not receive a valid HTTP request")
    top.__cause__ = middle
    return top


class TestTheFilter:
    def test_the_probes_traceback_is_dropped(self) -> None:
        assert _DropReadinessProbe().filter(_record(_probe_exception())) is False

    def test_a_real_handshake_failure_still_gets_through(self) -> None:
        """A client speaking the wrong protocol is a finding, not noise."""
        boom = RuntimeError("invalid Sec-WebSocket-Key header")
        assert _DropReadinessProbe().filter(_record(boom)) is True

    def test_a_cause_deeper_in_the_chain_is_still_matched(self) -> None:
        """websockets re-raises twice; the marker is not on the top exception."""
        deep = _probe_exception().__cause__
        assert deep is not None
        assert _DropReadinessProbe().filter(_record(deep)) is False

    def test_non_error_records_are_untouched(self) -> None:
        record = _record(None)
        record.levelno = logging.INFO
        assert _DropReadinessProbe().filter(record) is True

    def test_an_error_without_an_exception_is_untouched(self) -> None:
        assert _DropReadinessProbe().filter(_record(None)) is True


class TestWhereItIsInstalled:
    def test_only_the_websockets_server_logger_is_touched(self) -> None:
        """Not global: a live Twilio call is a websocket server too, and its
        handshake errors are worth keeping."""
        from turncall.evals.worker import _quieten_readiness_probe

        before = list(logging.getLogger().filters)
        _quieten_readiness_probe()
        target = logging.getLogger("websockets.server")
        try:
            assert any(isinstance(f, _DropReadinessProbe) for f in target.filters)
            assert list(logging.getLogger().filters) == before
        finally:
            target.filters = [
                f for f in target.filters if not isinstance(f, _DropReadinessProbe)
            ]
