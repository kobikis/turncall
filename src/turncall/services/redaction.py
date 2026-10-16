"""Redaction: PII replaced in what leaves the call (#313, ADR-0024).

A project turns it on (`PUT /v1/projects/{id}/redaction`, admin) and picks
entity types. From then on the event exit point (`events/emit.py`) passes
every call event's payload through `redact_payload` before it is stored or
sent to a webhook, so the transcript read, `transcript.final` and the
`call.ended` webhook carry `<PHONE_NUMBER>` where the caller said a number.
What the model hears is never touched: the pipeline's frames and context
never pass through here, and the two consumers that read the conversation on
the call's behalf (the warm-transfer briefing, post-call analysis) read the
raw copy in `services/live_transcript`.

Detection is Presidio with spaCy's small English model, run locally so no text
goes to a vendor to be redacted. Neither is imported until a project with
redaction on needs it: nothing at module level here imports them, and
`policy_for_project` answers None for a project with it off, so its events
never reach the detector. Building the detector takes 2-3s and analysing a
long transcript hundreds of ms, which on the event loop would be dead air on
every live call in the process (ADR-0004); both run on one dedicated worker
thread, which also keeps spaCy, whose thread safety is not promised, to one
thread.

What is analysed in a payload: every string, at any depth, except
- at the top level, TurnCall's own structural fields: ids, timestamps, URLs
  and enum-valued fields (`_structural`), which carry no caller text and which
  a detector could mangle (a digit run in a UUID read as a phone number);
- anywhere, a value that is a UUID or an ISO-8601 timestamp.
The top-level phone fields (`from_number`, `to_number`, `customer_number` and
friends) are replaced whole when PHONE_NUMBER is chosen and the value holds a
number, whether or not the detector would recognise its format; the call row
keeps the raw number (routing, the suppression list and campaigns read it).

A detector that fails withholds the text instead (`<REDACTED>`): an event never
leaves raw because redaction broke.
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
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
# Top-level payload fields that are TurnCall's own structure, never text.
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
        "ended_reason",
        "stage",
        "model",
        "provider",
        "transfer_mode",
        "answered_by",
        "recording_status",
        "agent_name",
        "agent_version",
    }
)
_STRUCTURAL_SUFFIXES = ("_id", "_ids", "_sid", "_at", "_url")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"
)
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class RedactionPolicy:
    """A project's redaction: which entity types to replace."""

    entities: tuple[str, ...]


def policy_of(enabled: bool, entities: list[str] | None) -> RedactionPolicy | None:
    """The policy a project's columns describe; None when redaction is off."""
    if enabled is not True:
        return None
    return RedactionPolicy(tuple(entities) if entities else DEFAULT_ENTITIES)


async def policy_for_project(
    session: AsyncSession, project_id: UUID
) -> RedactionPolicy | None:
    """The project's redaction, read now: a change applies from the next
    event on. None for a project with it off, or with no row."""
    from turncall.storage.models import ProjectRow

    project = await session.get(ProjectRow, project_id)
    if project is None:
        return None
    return policy_of(project.redaction_enabled, project.redaction_entities)


async def redact_payload(
    payload: dict[str, Any], policy: RedactionPolicy
) -> dict[str, Any]:
    """`payload` with the policy's entity types replaced by `<ENTITY_TYPE>`.

    Never mutates `payload`: the caller may still hold it. Runs on the
    redaction thread, off the event loop.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _executor(), _redact_with_detector, payload, policy.entities
        )
    except Exception:
        logger.exception("redaction_failed")
        return _walk(payload, policy.entities, lambda _text: WITHHELD)


# --- The detector: built lazily, used only on the redaction thread ---------


class Detector(Protocol):
    def redact(self, text: str, entities: tuple[str, ...]) -> str: ...


class RedactionUnavailable(RuntimeError):
    """The detector cannot be built in this install."""


_EXECUTOR: ThreadPoolExecutor | None = None
_DETECTOR: Detector | None = None


def _executor() -> ThreadPoolExecutor:
    """The one redaction thread, started the first time it is needed."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="redaction")
    return _EXECUTOR


def _detector() -> Detector:
    """Built once per process, on the redaction thread. One worker, so two
    first events cannot both build it."""
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = _build_detector()
        logger.info("redaction_detector_loaded", model=MODEL)
    return _DETECTOR


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


def _redact_with_detector(
    payload: dict[str, Any], entities: tuple[str, ...]
) -> dict[str, Any]:
    detector = _detector()
    return _walk(payload, entities, lambda text: detector.redact(text, entities))


# --- The walk: which strings are text --------------------------------------


def _walk(
    payload: dict[str, Any], entities: tuple[str, ...], redact: Any
) -> dict[str, Any]:
    """A copy of `payload` with every text string passed through `redact`."""

    def text(value: str) -> str:
        if not value.strip() or _shaped_structural(value):
            return value
        redacted: str = redact(value)
        return value if redacted == value else redacted

    def deep(value: Any) -> Any:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            return {k: deep(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [deep(v) for v in value]
        return value

    out: dict[str, Any] = {}
    for key, value in payload.items():
        if _structural(key):
            out[key] = value
        elif key in PHONE_FIELDS and isinstance(value, str):
            if PHONE in entities and len(_DIGIT.findall(value)) >= 7:
                out[key] = f"<{PHONE}>"
            else:
                out[key] = text(value)
        else:
            out[key] = deep(value)
    return out


def _structural(key: str) -> bool:
    return key in _STRUCTURAL_FIELDS or key.endswith(_STRUCTURAL_SUFFIXES)


def _shaped_structural(value: str) -> bool:
    """A UUID (canonical form) or an ISO-8601 timestamp, wherever it sits."""
    value = value.strip()
    return bool(_UUID.fullmatch(value) or _ISO_TIMESTAMP.fullmatch(value))
