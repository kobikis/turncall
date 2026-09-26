"""Every TTS provider composes its own text-transform list.

Prefactor for #134. One shared list works while the only transform is
markdown stripping, which is provider-independent. The pronunciation
transform is not: pipecat exposes it as a **classmethod on the TTS service
class**, because each provider needs its own markup generated from the same
IPA — inline phoneme blocks for one, SSML phoneme tags for another, an inline
pronunciation object for a third. A single shared list cannot carry that.

So composition goes through one pure helper, keyed by the service class, and
a structural guard keeps a fifth provider branch from quietly skipping it.
Prior art for the guard: test_kb_pipeline_wiring.py. #137.

test_tts_text_transforms.py is the behavioural companion: it constructs
all four real services and mirrors pipecat's runtime unpacking. This file
is structural — that composition goes through the helper at all, which no
amount of constructing can show.
"""

import ast
from pathlib import Path

import pytest

import turncall

_FACTORY = Path(turncall.__file__).parent / "orchestrator" / "pipeline_factory.py"
_HELPER = "_tts_text_transforms"


def _tts_service_classes() -> list[type]:
    from pipecat.services.cartesia.tts import CartesiaTTSService
    from pipecat.services.deepgram.tts import DeepgramTTSService
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
    from pipecat.services.openai.tts import OpenAITTSService

    return [
        DeepgramTTSService,
        ElevenLabsTTSService,
        OpenAITTSService,
        CartesiaTTSService,
    ]


@pytest.mark.unit
@pytest.mark.parametrize("service_cls", _tts_service_classes())
def test_every_provider_still_strips_markdown_first(service_cls: type) -> None:
    """LLMs emit markdown that TTS would otherwise read aloud literally
    ("asterisk asterisk"). No provider may lose that, and it stays first."""
    from pipecat.utils.text.transforms import strip_markdown

    from turncall.orchestrator.pipeline_factory import _tts_text_transforms

    transforms = _tts_text_transforms(service_cls)

    assert transforms[0] == ("*", strip_markdown), (
        f"{service_cls.__name__} no longer strips markdown first: {transforms}"
    )


@pytest.mark.unit
def test_the_helper_is_pure_and_needs_no_pipeline() -> None:
    """It is a plain function of the service class — callable in a unit test
    with nothing constructed, which is the point of routing through it."""
    from turncall.orchestrator.pipeline_factory import _tts_text_transforms

    first, second = (
        _tts_text_transforms(_tts_service_classes()[0]),
        _tts_text_transforms(_tts_service_classes()[0]),
    )
    assert first == second
    assert first is not second, (
        "the helper hands out one shared mutable list again — a provider that "
        "appends its own transform would corrupt every other provider's"
    )


def _tts_constructions() -> list[ast.Call]:
    """Every `<Something>TTSService(...)` built inside _create_tts_service."""
    tree = ast.parse(_FACTORY.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_create_tts_service"
    )
    return [
        c
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id.endswith("TTSService")
    ]


@pytest.mark.unit
def test_every_provider_branch_composes_through_the_helper() -> None:
    """The guard. A fifth provider added later cannot silently fall back to a
    shared literal — which would work today and drop its pronunciations the
    moment #134 lands."""
    calls = _tts_constructions()
    assert len(calls) >= 4, (
        f"expected a construction per TTS provider, found {len(calls)} — "
        "did the branching move?"
    )

    for call in calls:
        built = call.func.id  # type: ignore[union-attr]
        kwargs = {k.arg: k.value for k in call.keywords}
        assert "text_transforms" in kwargs, (
            f"{built} is constructed without text_transforms — it will read "
            "markdown aloud"
        )
        value = kwargs["text_transforms"]
        assert (
            isinstance(value, ast.Call) and getattr(value.func, "id", None) == _HELPER
        ), (
            f"{built} does not compose its transforms through {_HELPER}() — a "
            "shared list cannot carry the per-provider pronunciation transform"
        )
        assert [getattr(a, "id", None) for a in value.args] == [built], (
            f"{built} passes the wrong service class to {_HELPER}(): "
            f"{[getattr(a, 'id', None) for a in value.args]}"
        )
