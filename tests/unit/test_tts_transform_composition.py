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
    """The four providers, for per-class coverage below.

    A hardcoded list, but not the thing that catches a fifth provider — the
    structural guard walks the factory's own AST for that, so forgetting to
    extend this costs per-class coverage, never the guard.
    """
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


def _called_name(node: ast.expr) -> str | None:
    """The callee's name, whether `X(...)` or `module.X(...)`.

    Matching both spellings is `test_kb_pipeline_wiring.py`'s idiom. It
    matters here: a branch constructing through an aliased import would
    otherwise drop out of the list silently rather than fail loudly, and a
    guard that quietly stops looking is worse than no guard.
    """
    if isinstance(node, ast.Name):
        return node.id
    return getattr(node, "attr", None)


def _tts_constructions() -> list[ast.Call]:
    """Every `<Something>TTSService(...)` built inside _create_tts_service.

    Remaining blind spot, for whoever extends this in #134: a branch that
    constructs through a *variable* (`cls = OpenAITTSService; cls(...)`) is
    invisible to any name match. Nothing in this module does that, and the
    `len(calls) >= 4` floor catches it only if the total moves.
    """
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
        and (_called_name(c.func) or "").endswith("TTSService")
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
        built = _called_name(call.func)
        kwargs = {k.arg: k.value for k in call.keywords}
        assert "text_transforms" in kwargs, (
            f"{built} is constructed without text_transforms — it will read "
            "markdown aloud"
        )
        value = kwargs["text_transforms"]
        assert isinstance(value, ast.Call) and _called_name(value.func) == _HELPER, (
            f"{built} does not compose its transforms through {_HELPER}() — a "
            "shared list cannot carry the per-provider pronunciation transform"
        )
        passed = [_called_name(a) for a in value.args]
        assert passed == [built], (
            f"{built} passes the wrong service class to {_HELPER}(): {passed}"
        )
