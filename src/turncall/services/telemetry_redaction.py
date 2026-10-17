"""Redaction in logs and spans (#314, ADR-0024).

A log line or a span carries caller text when it is about a call or a text
conversation: the numbers a webhook handler logs, a transfer's target, the
transcript pipecat's observers log and put on its STT/LLM/TTS spans. With the
project's redaction on, those are redacted before they leave the process.

**Which project.** Where a call or a text conversation runs, the code that
knows the project says so (`for_project`): the call session, the inbound call
and text handlers, the API's key check, a campaign's dial, an eval run. That
sets a context variable, so it holds for the rest of that task and for every
task it starts (a call's pipeline, its tool calls, its post-call analysis). A
log line or span from outside any project (startup, a webhook for a number
nobody owns) is left as it is.

**How.** Not with the detector: a log line is written on the event loop, and
Presidio takes hundreds of milliseconds over a transcript-sized string (and
runs in a worker process of its own, see `services/redaction`). Here it is
regular expressions, for the types that have a reliable shape: phone numbers,
email addresses, card numbers (Luhn-checked), US SSNs and IBANs (checksum
validated), each only when the project chose it. A type without a shape
(`PERSON`, `LOCATION`, ...) is not found in logs and spans. UUIDs and ISO-8601
timestamps are never touched. A run of digits too long for one number is
split at its spaces into the numbers side by side (`_numbers`); a number
inside an email-shaped string (a WhatsApp JID, a SIP address) is still found
when the project chose phone numbers but not emails; and a bare run starting
with 0 or 1 is a phone only at eleven digits from 1 (+1's), a card only at
fifteen from 1 (UATP), so epoch timestamps and padded ids are left alone.

**Logs.** `install()` puts `redact_record` on loguru as its patcher, which runs
on the thread that logs, before the record is handed to an `enqueue=True` sink
(so the context variable is still there), and for the stdlib records the
`InterceptHandler` forwards. It redacts the message and every `extra` value.
An exception's traceback is rendered without variable values (loguru's
`diagnose` would print the locals, transcript and all), redacted and appended
to the message; the record's `exception` is then cleared.

**Spans.** `RedactingSpanProcessor` marks a span started inside a redacting
project's context with `turncall.redaction` (the entity types), and
`RedactingSpanExporter`, wrapped around the exporter, redacts a marked span's
attributes, event attributes and status description as it exports it. The
call's own `turncall.from_number` / `turncall.to_number` are left off its spans
altogether (`PIPECAT_TRACE_INCLUDE_PII` notwithstanding).
"""

from __future__ import annotations

import re
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger

from turncall.services.redaction import (
    DEFAULT_ENTITIES,
    PHONE,
    RedactionPolicy,
    policy_for_project,
)

if TYPE_CHECKING:
    from loguru import Record
    from sqlalchemy.ext.asyncio import AsyncSession

# The policy of the project whose call or conversation this context runs.
_SCOPE: ContextVar[RedactionPolicy | None] = ContextVar(
    "turncall_telemetry_redaction", default=None
)

# The span attribute that marks a span for the exporter to redact.
SPAN_ATTRIBUTE = "turncall.redaction"

EMAIL = "EMAIL_ADDRESS"
CARD = "CREDIT_CARD"
SSN = "US_SSN"
IBAN = "IBAN_CODE"


# --- Which project ----------------------------------------------------------


async def for_project(
    project_id: UUID,
    *,
    session: AsyncSession | None = None,
    sessions: Callable[[], AsyncSession] | None = None,
) -> RedactionPolicy | None:
    """From here on in this task, and in the tasks it starts, logs and spans
    follow `project_id`'s redaction. Returns the policy (None when off).

    The policy is read through redaction's cache, on `session` or on a short
    session of its own. If it cannot be read, the default types are redacted:
    a log line never goes out raw because the policy was unreadable."""
    try:
        policy = await policy_for_project(
            project_id, session=session, sessions=sessions
        )
    except Exception:
        policy = RedactionPolicy(DEFAULT_ENTITIES)
        _SCOPE.set(policy)
        logger.exception(
            "telemetry_redaction_policy_unread", project_id=str(project_id)
        )
        return policy
    _SCOPE.set(policy)
    return policy


@asynccontextmanager
async def scoped_to_project(
    project_id: UUID,
    *,
    session: AsyncSession | None = None,
    sessions: Callable[[], AsyncSession] | None = None,
) -> AsyncIterator[RedactionPolicy | None]:
    """`for_project` for a block only, for a loop that serves one project
    after another in one task (the campaign dialer)."""
    token = _SCOPE.set(_SCOPE.get())
    try:
        yield await for_project(project_id, session=session, sessions=sessions)
    finally:
        _SCOPE.reset(token)


def use(policy: RedactionPolicy | None) -> None:
    """`for_project` with the policy already read."""
    _SCOPE.set(policy)


def current() -> RedactionPolicy | None:
    """The redaction logs and spans follow here; None when there is none."""
    return _SCOPE.get()


# --- Redacting a string ----------------------------------------------------

# A run of digits and phone punctuation: a phone number, a card number, or
# several numbers side by side.
_RUN = r"(?<![\w+])\+?\(?\d[\d \t().-]{5,}\d\)?(?!\w)"
_PATTERN = re.compile(
    # Never touched: a UUID, an ISO-8601 date or timestamp.
    r"(?P<keep>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)"
    r"|(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
    r"|(?P<iban>\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b)"
    r"|(?P<ssn>(?<![\w-])\d{3}[- ]\d{2}[- ]\d{4}(?![\w-]))"
    rf"|(?P<digits>{_RUN})"
)
_DIGIT_RUN = re.compile(_RUN)
_SPACES = re.compile(r"([ \t]+)")
_NOT_DIGIT = re.compile(r"\D")
_DOTTED = re.compile(r"[\d.]+")
_GROUPS = re.compile(r"\d+")
_DOTTED_PHONE = re.compile(r"(?:\d{1,3}\.)?\d{3}\.\d{3}\.\d{4}")
# A card's most digits: no one number in a run is longer.
_MOST_DIGITS = 19


def redact_line(text: str, entities: Sequence[str]) -> str:
    """`text` with the entity types in `entities` that have a shape replaced
    by `<ENTITY_TYPE>`."""
    if not text or not entities:
        return text
    chosen = frozenset(entities)

    def numbers(match: re.Match[str]) -> str:
        return _numbers(match.group(0), chosen)

    def replace(match: re.Match[str]) -> str:
        found = match.group(0)
        kind = match.lastgroup
        if kind == "email":
            if EMAIL in chosen:
                return f"<{EMAIL}>"
            # Email-shaped, and maybe a number's: a WhatsApp JID
            # (`14155550132@s.whatsapp.net`), a SIP address.
            return _DIGIT_RUN.sub(numbers, found)
        if kind == "iban" and IBAN in chosen and _iban_valid(found):
            return f"<{IBAN}>"
        if kind == "ssn" and SSN in chosen:
            return f"<{SSN}>"
        if kind == "digits":
            return _numbers(found, chosen)
        return found

    return _PATTERN.sub(replace, text)


def _numbers(run: str, chosen: frozenset[str]) -> str:
    """A run of digits and phone punctuation, its numbers replaced. One number
    if it reads as one; else it is several side by side ("4155550132
    4155550133"), split at its spaces: from each group on, the longest stretch
    that reads as a number is replaced (a digit too many redacted rather than
    one left), and a group that starts none is left as it is."""
    whole = _number_type(run, chosen)
    if whole is not None:
        return whole
    parts = _SPACES.split(run)
    groups, gaps = parts[0::2], parts[1::2]
    if len(groups) == 1:
        return run
    out: list[str] = []
    i = 0
    while i < len(groups):
        ends: list[int] = []
        digits = 0
        for j in range(i, len(groups)):
            digits += len(_NOT_DIGIT.sub("", groups[j]))
            if digits > _MOST_DIGITS:
                break
            ends.append(j + 1)
        for end in reversed(ends):
            if i == 0 and end == len(groups):
                continue  # the whole run: not a number
            stretch = "".join(_interleave(groups[i:end], gaps[i : end - 1]))
            found = _number_type(stretch, chosen)
            if found is not None:
                out.append(found)
                i = end
                break
        else:
            out.append(groups[i])
            i += 1
        if i < len(groups):
            out.append(gaps[i - 1])
    return "".join(out)


def _interleave(groups: list[str], gaps: list[str]) -> list[str]:
    out = [groups[0]]
    for gap, group in zip(gaps, groups[1:], strict=True):
        out += [gap, group]
    return out


def _number_type(found: str, chosen: frozenset[str]) -> str | None:
    """`<PHONE_NUMBER>` or `<CREDIT_CARD>` when `found` reads as one of the
    chosen types, else None."""
    digits = _NOT_DIGIT.sub("", found)
    phone = PHONE in chosen and _a_phone(found, digits)
    if phone and found.startswith("+"):
        return f"<{PHONE}>"
    if CARD in chosen and _a_card(found, digits):
        return f"<{CARD}>"
    if phone:
        return f"<{PHONE}>"
    return None


def _a_phone(found: str, digits: str) -> bool:
    """Seven to fifteen digits (E.164's most), written as a number is: with a
    `+`, with separators, or ten digits or more bare (a WhatsApp id, a US
    number without its `+1`). Bare and starting with 1, only eleven digits:
    +1's country code and ten (no country code starts with 1 but +1's, and a
    US area code never does), so an epoch timestamp, seconds (ten digits) to
    nanoseconds, is not one. Digits and dots only must read 415.555.0132:
    not a decimal, a version or an IP address. Separated, one group has three
    digits or more: "10 20 30 40" is a list."""
    if not 7 <= len(digits) <= 15:
        return False
    if found == digits:
        return len(digits) >= 10 and (digits[0] != "1" or len(digits) == 11)
    if "." in found and _DOTTED.fullmatch(found):
        return bool(_DOTTED_PHONE.fullmatch(found))
    return found.startswith("+") or any(len(g) >= 3 for g in _GROUPS.findall(found))


def _a_card(found: str, digits: str) -> bool:
    """13 to 19 digits passing Luhn. Bare, it starts as a card network's do:
    2 to 6 (Mastercard, Amex, Visa, Discover...), or 1 at fifteen digits
    (UATP). A bare run from 0 or 1 is a padded id or an epoch timestamp."""
    if not (13 <= len(digits) <= 19 and _luhn(digits)):
        return False
    if found != digits:
        return True
    return digits[0] in "23456" or (digits[0] == "1" and len(digits) == 15)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _iban_valid(found: str) -> bool:
    """ISO 13616's check: the country and check digits moved to the end, the
    letters as numbers, mod 97 is 1."""
    iban = found.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def _redact_value(value: Any, entities: Sequence[str]) -> Any:
    """A copy of `value` with its strings redacted. Numbers, booleans and None
    pass; any other object is redacted as the string it logs as."""
    if isinstance(value, str):
        return redact_line(value, entities)
    if value is None or isinstance(value, (bool, int, float, UUID)):
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v, entities) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_value(v, entities) for v in value]
    shown = str(value)
    redacted = redact_line(shown, entities)
    return value if redacted == shown else redacted


# --- Logs -------------------------------------------------------------------


def redact_record(record: Record) -> None:
    """loguru's patcher: the record redacted in place when the project whose
    context logged it redacts."""
    policy = _SCOPE.get()
    if policy is None:
        return
    entities = policy.entities
    record["message"] = redact_line(record["message"], entities)
    extra = record["extra"]
    for key, value in list(extra.items()):
        extra[key] = _redact_value(value, entities)
    exception = record["exception"]
    if exception is not None and exception.type is not None:
        rendered = "".join(
            traceback.format_exception(
                exception.type, exception.value, exception.traceback
            )
        ).rstrip()
        record["message"] += "\n" + redact_line(rendered, entities)
        record["exception"] = None


def install() -> None:
    """Redact log records from here on, in this process (idempotent)."""
    logger.configure(patcher=redact_record)


# --- Spans ------------------------------------------------------------------


class RedactingSpanProcessor:
    """Marks a span started in a redacting project's context, for
    `RedactingSpanExporter`. On the starting thread, where the context
    variable is; the span is redacted at export, once its attributes are all
    set."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        policy = _SCOPE.get()
        if policy is not None:
            span.set_attribute(SPAN_ATTRIBUTE, list(policy.entities))

    def _on_ending(self, span: Any) -> None:
        pass

    def on_end(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class RedactingSpanExporter:
    """An exporter that redacts the spans `RedactingSpanProcessor` marked
    before handing them to the one it wraps."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def export(self, spans: Sequence[Any]) -> Any:
        return self._inner.export([redact_span(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))


def redact_span(span: Any) -> Any:
    """A marked span's copy with its text redacted; an unmarked one as is."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Status

    attributes = span.attributes or {}
    entities = attributes.get(SPAN_ATTRIBUTE)
    if not entities:
        return span
    status = span.status
    if status is not None and status.description:
        status = Status(status.status_code, redact_line(status.description, entities))
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_span_attributes(attributes, entities),
        events=[
            Event(
                event.name,
                _span_attributes(event.attributes or {}, entities),
                event.timestamp,
            )
            for event in span.events
        ],
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _span_attributes(attributes: Any, entities: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key == SPAN_ATTRIBUTE:
            out[key] = value
        elif isinstance(value, str):
            out[key] = redact_line(value, entities)
        elif isinstance(value, (list, tuple)):
            out[key] = tuple(
                redact_line(v, entities) if isinstance(v, str) else v for v in value
            )
        else:
            out[key] = value
    return out


def install_span_redaction(provider: Any) -> None:
    """Mark spans on `provider` (an SDK `TracerProvider`) for redaction."""
    add = getattr(provider, "add_span_processor", None)
    if add is not None:
        add(RedactingSpanProcessor())
