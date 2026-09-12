"""Every agent-config field should be read by something.

The most common defect in this codebase is a field that validates, stores,
round-trips through the API and is never read. It behaves perfectly as data —
it serialises into config_blob, survives a publish, appears in the OpenAPI
spec — and does nothing. The schema tests pass, because the schema works.

Known instances: `stt.extra`, `tts.extra` and `tts.speed` (all shipped inert),
`execution_mode: "async"`, `MCP_MAX_RESPONSE_BYTES`, and three columns on
`tool_invocations`. This is a crude structural guard against the next one.

It is deliberately loose — a bare `.field` match anywhere outside `domain/`
and `api/` counts — because the consuming code rebinds nested config to
locals (`vm_config.beep_max_await_seconds`), so requiring `voicemail_detection.`
as a prefix produced twenty false positives. A generic name can therefore
pass on a coincidental match; this catches "wired to nothing", not "wired
wrongly". What it really buys is the list below: a new field has to be either
consumed or explicitly declared inert, which forces the decision at the moment
someone adds it.
"""

import typing
from pathlib import Path

import pytest

import turncall
from turncall.domain.models import AgentConfig

# Fields that are genuinely read by nothing, recorded so the test stays green
# while the gap stays visible. Removing an entry is how you claim it's wired.
NOT_CONSUMED: dict[str, str] = {
    "AgentConfig.silence_timeout_ms": (
        "accepted and validated 200-5000, read nowhere — the pipeline uses "
        "smart_turn_stop_secs instead"
    ),
    "AgentConfig.interruption_enabled": (
        "accepted, read nowhere — barge-in cannot actually be turned off"
    ),
    "AgentConfig.max_call_duration_seconds": (
        "accepted and validated 60-14400, read nowhere — nothing caps a call "
        "at the configured duration"
    ),
    "AgentConfig.knowledge_bases": (
        "vestigial: KBs attach via /agents/{id}/knowledge-bases, and the API "
        "schema forbids the field outright"
    ),
    "VoicemailConfig.voicemail_expected_duration_seconds": (
        "accepted, read nowhere — the other voicemail fields do reach Pipecat"
    ),
    "AgentConfig.analysis": (
        "consumed, but through the raw blob rather than the attribute: "
        "call_analysis_trigger rebuilds it with "
        "AnalysisConfig(**agent_config_blob.get('analysis', {})). Its own "
        "fields are read normally, so only this hop is invisible here"
    ),
    "ToolDefinition.is_builtin": (
        "set on construction, never read — dispatch matches BUILTIN_TOOL_NAMES "
        "by name instead"
    ),
}


def _consumer_source() -> str:
    """Everything outside the layers that only declare and serialise config."""
    src = Path(turncall.__file__).parent
    return "\n".join(
        path.read_text()
        for path in src.rglob("*.py")
        if not {"domain", "api"} & set(path.relative_to(src).parts)
    )


def _nested_model(annotation: typing.Any) -> typing.Any:
    """The config model behind a field, whether it's held directly or in a list."""
    if getattr(annotation, "model_fields", None) is not None:
        return annotation
    for arg in typing.get_args(annotation) or ():
        if getattr(arg, "model_fields", None) is not None:
            return arg
    return None


def _walk(model: typing.Any, seen: set[str]) -> list[tuple[str, str]]:
    """(owner, field) for every field reachable from a config model."""
    out: list[tuple[str, str]] = []
    if model.__name__ in seen:
        return out
    seen.add(model.__name__)
    for name, field in model.model_fields.items():
        out.append((model.__name__, name))
        nested = _nested_model(field.annotation)
        if nested is not None:
            out.extend(_walk(nested, seen))
    return out


@pytest.mark.unit
def test_every_config_field_is_read_somewhere() -> None:
    source = _consumer_source()
    fields = _walk(AgentConfig, set())
    assert len(fields) > 50, f"walk found only {len(fields)} fields — did it break?"

    unread = [
        f"{owner}.{name}"
        for owner, name in fields
        if f".{name}" not in source and f"{owner}.{name}" not in NOT_CONSUMED
    ]

    assert not unread, (
        "config fields nothing reads — wire them up, or add them to "
        f"NOT_CONSUMED with the reason: {unread}"
    )


@pytest.mark.unit
def test_the_inert_list_does_not_outlive_its_entries() -> None:
    """An entry that has since been wired up should be removed, or the list
    stops meaning anything."""
    source = _consumer_source()
    now_consumed = [
        key for key in NOT_CONSUMED if f".{key.split('.')[-1]}" in source
    ]
    assert not now_consumed, (
        f"these are consumed now — drop them from NOT_CONSUMED: {now_consumed}"
    )
