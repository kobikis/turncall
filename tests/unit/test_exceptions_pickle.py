"""Every TurnCall exception survives pickling, so a log line carrying one is
written (#352).

Both log sinks are `enqueue=True`: loguru pickles each record, a logged
exception included, and rebuilds it on the writer thread. An exception whose
`__init__` takes anything but its message could not be rebuilt, and loguru
dropped the whole line.
"""

import importlib
import inspect
import pickle
import pkgutil
import uuid

import pytest
from loguru import logger

import turncall
from turncall.adapters.telephony.telnyx_adapter import (
    TelnyxApiError,
    TelnyxOrderFailed,
)
from turncall.api.errors import (
    ApiError,
    BadRequestError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    InvalidStateTransitionError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
)
from turncall.domain.errors import PicklableError
from turncall.services.credentials import MissingProviderKeyError
from turncall.services.outbound_dial import DialNotRecorded

pytestmark = pytest.mark.unit


def _turncall_exceptions() -> list[type[BaseException]]:
    found: dict[str, type[BaseException]] = {}
    for info in pkgutil.walk_packages(turncall.__path__, "turncall."):
        module = importlib.import_module(info.name)
        for cls in vars(module).values():
            if (
                inspect.isclass(cls)
                and issubclass(cls, BaseException)
                and cls.__module__ == module.__name__
            ):
                found[f"{cls.__module__}.{cls.__qualname__}"] = cls
    return [found[name] for name in sorted(found)]


def test_every_exception_with_an_init_of_its_own_is_picklable() -> None:
    exceptions = _turncall_exceptions()
    assert ApiError in exceptions  # the walk found them
    own_init = [
        cls
        for cls in exceptions
        if any(
            "__init__" in vars(base)
            for base in cls.__mro__
            if base.__module__.startswith("turncall.")
        )
    ]
    assert [c for c in own_init if not issubclass(c, PicklableError)] == []


@pytest.mark.parametrize(
    "exc",
    [
        TelnyxApiError(422, ["10015"], "Invalid phone number"),
        TelnyxOrderFailed("order-1", "+15550001234"),
        ApiError(400, ErrorCode.VALIDATION_ERROR, "bad", {"field": "x"}),
        BadRequestError("bad", {"field": "x"}),
        RateLimitedError("slow down"),
        NotFoundError("Agent", "a1"),
        ConflictError("taken"),
        UnauthorizedError(),
        ForbiddenError("no"),
        InvalidStateTransitionError("ended", "ringing"),
        MissingProviderKeyError(["deepgram", "openai"]),
        DialNotRecorded(uuid.uuid4(), "CA123"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_an_exception_comes_back_whole(exc: BaseException) -> None:
    back = pickle.loads(pickle.dumps(exc))  # noqa: S301 - our own bytes

    assert type(back) is type(exc)
    assert back.args == exc.args
    assert str(back) == str(exc)
    assert vars(back) == vars(exc)


async def test_a_logged_telnyx_error_reaches_a_queued_sink() -> None:
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", serialize=True, enqueue=True)
    try:
        try:
            raise TelnyxApiError(422, ["10015"], "Invalid phone number")
        except TelnyxApiError:
            logger.exception("telnyx refused")
        await logger.complete()
    finally:
        logger.remove(sink)

    assert len(lines) == 1
    assert "Telnyx 422: Invalid phone number" in lines[0]
