"""Redaction: PII replaced in what leaves the call (#313, ADR-0024).

A project turns it on (`PUT /v1/projects/{id}/redaction`, admin) and picks
entity types. From then on the event exit point (`events/emit.py`) passes
every call event's payload through here before it is stored or sent to a
webhook, so the transcript read, `transcript.final` and the `call.ended`
webhook carry `<PHONE_NUMBER>` where the caller said a number. Post-call
analysis and takeaways are redacted the same way before they are stored on
the call. What the model hears is never touched: the pipeline's frames and
context never pass through here, and the two consumers that read the
conversation on the call's behalf (the warm-transfer briefing, post-call
analysis) read the raw copy in `services/live_transcript`.

**The policy** is read through a per-process cache (`POLICY_TTL_SECONDS`). A
`PUT` on this process replaces its entry at once; another replica picks the
change up within the TTL.

**What is analysed** in a payload: every string, at any depth, and every
integer of `NUMBER_DIGITS` digits or more below the top level (a phone or card
number a tool's arguments or the call's metadata carry as a JSON number, the
rule `redact_json` applies), except
- at the top level, TurnCall's own structural and identifier fields: ids,
  timestamps, URLs, enum-valued fields, provider and tool names
  (`_structural`), plus a few event-specific ones (`_IDENTIFIERS_BY_EVENT`:
  `provider.failover`'s `from`/`to`, `call.ended`'s and `avatar.unavailable`'s
  `reason`). They carry no caller text, and a detector could mangle them (a
  digit run in a UUID read as a phone number, a provider name read as an
  ORGANIZATION);
- at the top level, a number: TurnCall's own measure (`duration_ms`, a
  count), never what the caller said;
- anywhere, a value that is a UUID or an ISO-8601 timestamp.
The top-level phone fields (`from_number`, `to_number`, `customer_number` and
friends) are replaced whole when PHONE_NUMBER is chosen and the value holds a
number, whether or not the detector would recognise its format; the call row
keeps the raw number (routing, the suppression list and campaigns read it).
None of that needs the detector: a payload with no text in it is redacted
right here, synchronously, and never reaches it.

Those key rules are for TurnCall's own payloads. JSON whose keys the user
chose (a tool's arguments and result, a takeaway) goes through `redact_json`,
which analyses every string whatever its key; a lone string, through
`redact_text` (#314).

**The detector** is Presidio with spaCy's small English model, run locally so
no text goes to a vendor to be redacted. It runs in a worker process of its
own (`_Engine`), not a thread: Presidio is mostly Python and holds the GIL, so
on a thread it took the event loop's p99 lag from ~7-12 ms to ~16-46 ms with
20-40 calls in the process (measured, ADR-0024), which is audio jitter on
every live call (ADR-0004). In a process it left the loop's lag about where
it was without redaction. Nothing imports Presidio in the
app's process at all; the worker does, once, when it starts.

The worker starts the first time a redacting project needs it, or earlier:
`warm()` is called at startup when any project redacts and when a project
turns redaction on, so the 2-12 s model load is not paid by a call. A load
that fails opens a circuit breaker: for `RETRY_AFTER_SECONDS` every text is
withheld (`<REDACTED>`) at once, logged once per window, and then the load is
tried again. Work is bounded (`MAX_PENDING`): past it, text is withheld rather
than queued behind minutes of backlog, and a redaction slower than
`SLOW_SECONDS` is logged with the backlog it waited behind.

An event never leaves raw because redaction broke: a detector that fails, is
unavailable or is saturated withholds the text instead.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# What a project gets with redaction on and no entities chosen (#313). PERSON
# is opt-in: the small model reads "Ava from Acme Dental" as "Ava from
# <PERSON>", missing the name and taking the company.
DEFAULT_ENTITIES: tuple[str, ...] = (
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
    "US_SSN",
    "IBAN_CODE",
)

# What a project may choose: Presidio's English recognizers with the small
# model. Static, so validating a request never loads Presidio;
# `test_redaction_detector.py` holds it to what the detector supports. spaCy's
# own `EMAIL` and `ID` labels are left out: artefacts of its NER, not types.
SUPPORTED_ENTITIES: frozenset[str] = frozenset(
    {
        "AGE",
        "CREDIT_CARD",
        "CRYPTO",
        "DATE_TIME",
        "EMAIL_ADDRESS",
        "IBAN_CODE",
        "IP_ADDRESS",
        "LOCATION",
        "MAC_ADDRESS",
        "MEDICAL_LICENSE",
        "NRP",
        "ORGANIZATION",
        "PERSON",
        "PHONE_NUMBER",
        "UK_NHS",
        "URL",
        "US_BANK_NUMBER",
        "US_DRIVER_LICENSE",
        "US_ITIN",
        "US_PASSPORT",
        "US_SSN",
    }
)

MODEL = "en_core_web_sm"
WITHHELD = "<REDACTED>"
PHONE = "PHONE_NUMBER"

# How long a project's policy is trusted before it is read again. A change made
# through another replica applies here within this long.
POLICY_TTL_SECONDS = 30.0
# After the detector failed to load: withhold for this long, then try again.
RETRY_AFTER_SECONDS = 300.0
# Texts in flight (queued or running in the worker). Past it, withheld.
MAX_PENDING = 256
# A redaction that took longer than this is logged, with the backlog.
SLOW_SECONDS = 1.0

# Integers this long or longer are analysed, in free JSON and below a
# payload's top level (`_deep`): the fewest digits a phone number has. A
# count, a duration or a party size is shorter and never reaches the detector.
NUMBER_DIGITS = 7

# Top-level payload fields holding a phone number (decision 5 on #313).
PHONE_FIELDS = frozenset(
    {
        "from_number",
        "to_number",
        "customer_number",
        "turncall_number",
        "target_number",
        "number",
    }
)
# Top-level payload fields that are TurnCall's own structure or an identifier,
# never caller text, whatever the event (the field inventory in #313's notes).
_STRUCTURAL_FIELDS = frozenset(
    {
        "id",
        "timestamp",
        "role",
        "source",
        "status",
        "direction",
        "transport",
        "channel",
        "event",
        "kind",
        "modality",
        "ended_reason",
        "stage",
        "model",
        "provider",
        "transfer_mode",
        "answered_by",
        "recording_status",
        "recording_duration",
        "agent_name",
        "agent_version",
        "tool_name",
        "twilio_status",
        "internal_status",
        "hangup_cause",
        "hangup_source",
        "sip_hangup_cause",
        "missing_keys",
    }
)
_STRUCTURAL_SUFFIXES = ("_id", "_ids", "_sid", "_at", "_url")
# Fields that are identifiers in these events only: elsewhere the same name
# carries text (`reason` on `call.transferred` is the model's own words).
_IDENTIFIERS_BY_EVENT: dict[str, frozenset[str]] = {
    "provider.failover": frozenset({"from", "to"}),
    "call.failed": frozenset({"reason"}),
    "call.rejected": frozenset({"reason"}),
    "avatar.unavailable": frozenset({"reason"}),
}
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"
)
_DIGIT = re.compile(r"\d")
_PHONE_SHAPED = re.compile(r"\s*(?:[a-z]+:)?\+?[\d\s().-]+\s*")


# --- The policy -------------------------------------------------------------


@dataclass(frozen=True)
class RedactionPolicy:
    """A project's redaction: which entity types to replace."""

    entities: tuple[str, ...]


def policy_of(enabled: bool, entities: list[str] | None) -> RedactionPolicy | None:
    """The policy a project's columns describe; None when redaction is off.
    A stored entity type this build no longer knows is dropped (and logged by
    the project read), never an error."""
    if enabled is not True:
        return None
    known = tuple(e for e in entities or () if e in SUPPORTED_ENTITIES)
    return RedactionPolicy(known or DEFAULT_ENTITIES)


_POLICIES: dict[UUID, tuple[float, RedactionPolicy | None]] = {}


def remember_policy(project_id: UUID, policy: RedactionPolicy | None) -> None:
    """The project's policy as just written on this process (the `PUT`), so
    this process applies it from the next event rather than after the TTL."""
    _POLICIES[project_id] = (time.monotonic() + POLICY_TTL_SECONDS, policy)


def forget_policies() -> None:
    _POLICIES.clear()


async def policy_for_project(
    project_id: UUID,
    *,
    session: AsyncSession | None = None,
    sessions: Callable[[], AsyncSession] | None = None,
) -> RedactionPolicy | None:
    """The project's redaction, None when it is off (or there is no project).

    Cached for `POLICY_TTL_SECONDS`. On a miss it is read on `session` when
    given, else on a short session of its own from `sessions` (the app's
    factory by default), closed before this returns — so a caller about to
    wait on the detector holds no connection for it.
    """
    cached = _POLICIES.get(project_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    if session is not None:
        policy = await _read_policy(session, project_id)
    else:
        policy = await _read_policy_alone(project_id, sessions)
    remember_policy(project_id, policy)
    return policy


async def _read_policy_alone(
    project_id: UUID, sessions: Callable[[], AsyncSession] | None = None
) -> RedactionPolicy | None:
    if sessions is None:
        from turncall.storage.database import get_session_factory

        sessions = get_session_factory()
    async with sessions() as own:
        return await _read_policy(own, project_id)


async def _read_policy(
    session: AsyncSession, project_id: UUID
) -> RedactionPolicy | None:
    from turncall.storage.models import ProjectRow

    project = await session.get(ProjectRow, project_id)
    if project is None:
        return None
    return policy_of(project.redaction_enabled, project.redaction_entities)


# --- Redacting --------------------------------------------------------------


def texts_in(
    payload: dict[str, Any], *, event_type: str | None = None, keep: Iterable[str] = ()
) -> list[str]:
    """The strings of `payload` that are text, which only the detector can
    redact, and the long integers below its top level, as their digits.
    Whatever the policy: empty means the payload is ids, short numbers and
    structure only."""
    found: list[str] = []

    def collect(text: str) -> str:
        found.append(text)
        return text

    _walk(payload, (), event_type=event_type, keep=frozenset(keep), redact=collect)
    return found


def text_fields(
    payload: dict[str, Any], *, event_type: str | None = None
) -> frozenset[str]:
    """The top-level fields of `payload` holding text: those redaction
    analyses (a handoff's `reason` and `context_payload`, a transfer's
    `reason` and `transfer_message`, an error's text), where the rest are ids,
    numbers and structure. Retention clears these when a call's transcript
    expires (#316): they quote, or were made from, the conversation."""
    return frozenset(
        key
        for key, value in payload.items()
        if texts_in({key: value}, event_type=event_type)
    )


def redact_fields(
    payload: dict[str, Any],
    policy: RedactionPolicy,
    *,
    event_type: str | None = None,
) -> dict[str, Any]:
    """`payload` redacted without the detector: phone fields replaced, and any
    text withheld. Synchronous, for a caller that must not wait (inside a
    transaction); the exit point uses it for payloads with no text."""
    return _walk(
        payload,
        policy.entities,
        event_type=event_type,
        keep=frozenset(),
        redact=lambda _text: WITHHELD,
    )


async def redact_payload(
    payload: dict[str, Any],
    policy: RedactionPolicy,
    *,
    event_type: str | None = None,
    keep: Iterable[str] = (),
    strict: bool = False,
) -> dict[str, Any]:
    """`payload` with the policy's entity types replaced by `<ENTITY_TYPE>`.

    `keep`: top-level keys passed through as they are, for values read back
    from this project's stored events, which were redacted when stored. Never
    mutates `payload`: the caller may still hold it. Its text is analysed in
    the redaction worker, off the event loop; a payload with no text never
    reaches the worker at all. `strict`: raise `RedactionWithheld` rather than
    withhold the text when the detector does not answer.
    """
    keep = frozenset(keep)
    texts = texts_in(payload, event_type=event_type, keep=keep)
    replaced = await _detect(texts, policy.entities, strict) if texts else {}
    return _walk(
        payload,
        policy.entities,
        event_type=event_type,
        keep=keep,
        redact=lambda text: replaced.get(text, WITHHELD),
    )


async def redact_json(
    value: Any, policy: RedactionPolicy, *, strict: bool = False
) -> Any:
    """Free JSON (`value`: any JSON value) with the policy's entity types
    replaced: a tool's arguments or result, a takeaway (#314).

    Unlike `redact_payload`, nothing is skipped by its key: the keys are the
    user's, not TurnCall's envelope, so a field named `customer_id`,
    `callback_url` or `number` may hold what the caller said, and a phone
    number in it is the detector's to find rather than replaced whole by its
    field's name. Keys themselves are not analysed. UUIDs and ISO-8601
    timestamps are skipped by their shape, as everywhere. An integer of
    `NUMBER_DIGITS` digits or more is analysed as the digits it is written
    with (`{"card": 4111111111111111}`, `{"phone": 4155550132}`): one the
    detector finds becomes its `<ENTITY_TYPE>` string, any other stays the
    number it was. The same worker, the same failure mode (withheld, never
    raw, or `RedactionWithheld` when `strict`), and never mutates `value`.
    """
    texts: list[str] = []

    def collect(text: str) -> str:
        texts.append(text)
        return text

    _deep(value, collect)
    replaced = await _detect(texts, policy.entities, strict) if texts else {}
    return _deep(value, lambda text: replaced.get(text, WITHHELD))


async def redact_text(text: str, policy: RedactionPolicy) -> str:
    """One plain string redacted: an SMS row's content, a chat message."""
    redacted: str = await redact_json(text, policy)
    return redacted


class RedactionWithheld(RuntimeError):
    """The detector did not answer (unavailable, saturated or failed), for a
    `strict` caller: one rewriting stored rows, which would rather try again
    later than store `<REDACTED>` for good."""


async def _detect(
    texts: list[str], entities: tuple[str, ...], strict: bool = False
) -> dict[str, str]:
    """Each distinct text, redacted; a text left out is withheld."""
    unique = list(dict.fromkeys(texts))
    redacted = await ENGINE.redact(unique, entities)
    if redacted is None:
        if strict:
            raise RedactionWithheld(f"{len(unique)} text(s) not redacted")
        return {}
    return dict(zip(unique, redacted, strict=True))


# --- The walk: which strings are text --------------------------------------


def _walk(
    payload: dict[str, Any],
    entities: tuple[str, ...],
    *,
    event_type: str | None,
    keep: frozenset[str],
    redact: Callable[[str], str],
) -> dict[str, Any]:
    """A copy of `payload` with every text string passed through `redact`."""
    identifiers = _IDENTIFIERS_BY_EVENT.get(event_type or "", frozenset())
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in keep or key in identifiers or _structural(key) or _a_measure(value):
            out[key] = value
        elif key in PHONE_FIELDS and isinstance(value, str) and _a_number(value):
            # A number and nothing else: no other type to find in it.
            out[key] = f"<{PHONE}>" if PHONE in entities else value
        else:
            out[key] = _deep(value, redact)
    return out


def _deep(value: Any, redact: Callable[[str], str]) -> Any:
    """`value` with its text passed through `redact`, and its long integers
    too, as the digits they are written with: kept as numbers unless
    redacted. The one rule for free JSON (`redact_json`) and for what a
    payload carries (`_walk`)."""
    if isinstance(value, str):
        if not value.strip() or _shaped_structural(value):
            return value
        return redact(value)
    if _a_long_integer(value):
        written = str(value)
        redacted = redact(written)
        return value if redacted == written else redacted
    if isinstance(value, dict):
        return {k: _deep(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep(v, redact) for v in value]
    return value


def _a_long_integer(value: Any) -> bool:
    """An integer that may be a phone, card or SSN number: no bool, no float
    (an amount), and `NUMBER_DIGITS` digits or more (a count is fewer)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and len(str(abs(value))) >= NUMBER_DIGITS
    )


def _a_measure(value: Any) -> bool:
    """A number at a payload's top level: TurnCall's own (`duration_ms`, a
    count, a flag). The caller's numbers come nested, in a tool's arguments
    or the call's metadata, where `_deep` analyses them."""
    return isinstance(value, (int, float))


def _a_number(value: str) -> bool:
    """A phone number, however written: digits and punctuation, at least seven
    digits, after an optional scheme (`whatsapp:+1...`, `tel:`; a WhatsApp id
    has no `+` at all). A WebRTC call's "webrtc" or a SIP address is text."""
    return bool(_PHONE_SHAPED.fullmatch(value)) and len(_DIGIT.findall(value)) >= 7


def _structural(key: str) -> bool:
    return key in _STRUCTURAL_FIELDS or key.endswith(_STRUCTURAL_SUFFIXES)


def _shaped_structural(value: str) -> bool:
    """A UUID (canonical form) or an ISO-8601 timestamp, wherever it sits."""
    value = value.strip()
    return bool(_UUID.fullmatch(value) or _ISO_TIMESTAMP.fullmatch(value))


# --- The detector, in its worker process ------------------------------------


class Detector(Protocol):
    def redact(self, text: str, entities: tuple[str, ...]) -> str: ...


class RedactionUnavailable(RuntimeError):
    """The detector cannot be built in this install."""


class _PresidioDetector:
    def __init__(self, analyzer: Any, anonymizer: Any) -> None:
        self._analyzer = analyzer
        self._anonymizer = anonymizer

    def redact(self, text: str, entities: tuple[str, ...]) -> str:
        found = self._analyzer.analyze(
            text=text, language="en", entities=list(entities)
        )
        if not found:
            return text
        return str(self._anonymizer.anonymize(text=text, analyzer_results=found).text)


def _build_detector() -> Detector:
    """Presidio over spaCy's small model, with both of its runtime downloads
    off: a missing model is an error here rather than a pip install mid-call,
    and the email recognizer's suffix list is the copy tldextract ships
    rather than a fetch from GitHub (it reads the variable at import)."""
    os.environ.setdefault("TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS", "")
    import spacy.util

    if not spacy.util.is_package(MODEL):
        raise RedactionUnavailable(
            f"spaCy model {MODEL} is not installed; it ships with TurnCall's "
            "dependencies (pyproject.toml), so this install is incomplete"
        )
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    # Never AnalyzerEngine() bare: its default is the large model.
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": MODEL}],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    return _PresidioDetector(analyzer, AnonymizerEngine())


# In the worker: its detector, built by `_worker_load` (the first job it runs).
_DETECTOR: Detector | None = None


def _worker_load() -> None:
    """Build the worker's detector, raising if this install cannot."""
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = _build_detector()
        # The first analysis compiles what the build left lazy (~120 ms).
        _DETECTOR.redact("warm 415 555 0132", (PHONE,))


def _worker_redact(texts: list[str], entities: tuple[str, ...]) -> list[str]:
    _worker_load()
    assert _DETECTOR is not None
    return [_DETECTOR.redact(text, entities) for text in texts]


def _new_pool() -> Executor:
    """One worker process, started fresh (`spawn`): forking a process that runs
    an event loop and threads copies their locks mid-use. One worker holds the
    model once (~200 MB) and keeps spaCy to one thread; it does about 200
    utterances a second."""
    return ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    )


class _Engine:
    """The app's side of the redaction worker: starts it, loads the model in it
    once, sends it text, and fails closed. Thread- and loop-agnostic state
    (`concurrent.futures`), so one engine serves every event loop the process
    runs (the test suite runs many)."""

    def __init__(self) -> None:
        self._pool: Executor | None = None
        self._loaded: Future[None] | None = None
        self._closed_until = 0.0  # the breaker is open until then
        self._withheld_logged_until = 0.0
        self._saturated_logged_until = 0.0
        self._pending = 0

    def warm(self) -> Future[None] | None:
        """Start the worker and load its model, without waiting. None while
        the breaker is open."""
        if time.monotonic() < self._closed_until:
            return None
        if self._loaded is not None and _failed(self._loaded):
            self.shutdown()  # the back-off is over: load it again
        if self._loaded is None:
            self._pool = _new_pool()
            self._loaded = self._pool.submit(_worker_load)
            self._loaded.add_done_callback(self._on_loaded)
        return self._loaded

    async def ready(self) -> bool:
        """Wait for the model to load; whether it did."""
        loaded = self.warm()
        if loaded is None:
            return False
        try:
            await asyncio.wrap_future(loaded)
        except Exception:
            return False
        return True

    async def redact(
        self, texts: list[str], entities: tuple[str, ...]
    ) -> list[str] | None:
        """The texts redacted, or None: withhold them."""
        loaded = self.warm()
        if loaded is None:
            self._log_withheld()
            return None
        if self._pending + len(texts) > MAX_PENDING and self._pending:
            self._log_saturated(len(texts))
            return None
        self._pending += len(texts)
        started = time.monotonic()
        try:
            await asyncio.wrap_future(loaded)
            assert self._pool is not None
            job = self._pool.submit(_worker_redact, texts, entities)
            return await asyncio.wrap_future(job)
        except BrokenProcessPool:
            # The worker died (killed, out of memory). Start over after the
            # back-off rather than respawning it under every event.
            self._open("redaction_worker_died")
            return None
        except Exception:
            if _failed(loaded):
                return None  # the load failed: `_on_loaded` opened the breaker
            logger.exception("redaction_failed", texts=len(texts))
            return None
        finally:
            self._pending -= len(texts)
            took = time.monotonic() - started
            if took > SLOW_SECONDS:
                logger.warning(
                    "redaction_slow",
                    seconds=round(took, 2),
                    texts=len(texts),
                    chars=sum(len(t) for t in texts),
                    backlog=self._pending,
                )

    def shutdown(self) -> None:
        pool, self._pool, self._loaded = self._pool, None, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _on_loaded(self, loaded: Future[None]) -> None:
        """On the worker's result thread: never touches the pool from here."""
        if loaded.cancelled():
            return
        error = loaded.exception()
        if error is None:
            logger.info("redaction_detector_loaded", model=MODEL)
            return
        logger.opt(exception=error).error(
            "redaction_unavailable", retry_in_seconds=RETRY_AFTER_SECONDS
        )
        self._closed_until = time.monotonic() + RETRY_AFTER_SECONDS

    def _open(self, event: str) -> None:
        logger.error(event, retry_in_seconds=RETRY_AFTER_SECONDS)
        self._closed_until = time.monotonic() + RETRY_AFTER_SECONDS
        self.shutdown()

    def _log_withheld(self) -> None:
        now = time.monotonic()
        if now >= self._withheld_logged_until:
            self._withheld_logged_until = self._closed_until
            logger.warning(
                "redaction_withheld_detector_unavailable",
                retry_in_seconds=round(self._closed_until - now),
            )

    def _log_saturated(self, texts: int) -> None:
        now = time.monotonic()
        if now >= self._saturated_logged_until:
            self._saturated_logged_until = now + 60
            logger.error(
                "redaction_backlog_full_withheld",
                backlog=self._pending,
                texts=texts,
                max_pending=MAX_PENDING,
            )


def _failed(loaded: Future[None]) -> bool:
    return loaded.done() and (loaded.cancelled() or loaded.exception() is not None)


ENGINE = _Engine()


def warm() -> None:
    """Load the detector in the background, so no call pays for it. Called at
    startup when a project redacts, and when a project turns redaction on."""
    ENGINE.warm()


async def ready() -> bool:
    """Wait until the detector is loaded; whether it loaded."""
    return await ENGINE.ready()


def shutdown() -> None:
    ENGINE.shutdown()


async def warm_if_any_project_redacts() -> None:
    """At startup: warm the detector if any project here has redaction on."""
    from sqlalchemy import select

    from turncall.storage.database import get_session_factory
    from turncall.storage.models import ProjectRow

    try:
        async with get_session_factory()() as session:
            found = await session.scalar(
                select(ProjectRow.id).where(ProjectRow.redaction_enabled).limit(1)
            )
    except Exception:
        logger.exception("redaction_warm_check_failed")
        return
    if found is not None:
        warm()
