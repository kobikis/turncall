"""The voicemail classifier, against a real LLM (pipecat 1.12).

1.12 replaced the detector's parallel-pipeline LLM branch with a `Classifier`
asked one `ChoiceQuestion`. That is a different prompt, a different parse and a
different failure mode from the prose classification TurnCall shipped on 1.11,
and nothing hermetic can tell us whether it still separates a person from an
answering machine — a `ChoiceResult` parsed from a real model's JSON is the
only thing that answers it.

Cheap: one classifier call per case against gpt-4o-mini, the same model and
the same deterministic temperature the pipeline pins for this.

Live: needs OPENAI_API_KEY. Skipped without it.
"""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

# What the transcript looks like in each case. The fragments are the point of
# the third and fourth: 1.12 deliberately refuses to act on one ("hi, this is
# Sam" is what a person says *and* how a greeting starts), and only the silence
# that follows tells them apart. They are recorded here as the reason the
# detector waits `decision_timeout` before acting, not as assertions about a
# verdict the model is not meant to be sure of.
CASES = [
    ("you've reached Dana. Please leave a message after the tone.", "voicemail"),
    ("I'm not available right now, call me back", "voicemail"),
    ("hello? who is this?", "conversation"),
    ("yeah, John speaking", "conversation"),
]


def _classifier(openai_key: str):
    """Built the way `pipeline_factory` builds it: same model, same pinned
    temperature, reasoning off."""
    from pipecat.classifiers.llm.classifier import LLMClassifier
    from pipecat.services.openai.llm import OpenAILLMService

    return LLMClassifier(
        llm=OpenAILLMService(
            api_key=openai_key,
            settings=OpenAILLMService.Settings(
                model="gpt-4o-mini", extra={"temperature": 0.1}
            ),
        )
    )


@pytest.mark.parametrize("transcript,expected", CASES, ids=[c[0][:24] for c in CASES])
async def test_the_classifier_separates_a_person_from_an_answering_machine(
    openai_key: str, transcript: str, expected: str
) -> None:
    from pipecat.extensions.voicemail.voicemail_detector import VOICEMAIL_QUESTION

    results = await _classifier(openai_key).choice(
        transcript, {"voicemail": VOICEMAIL_QUESTION}
    )
    result = results["voicemail"]

    assert result.choice == expected, (
        f"{transcript!r} classified as {result.choice!r} "
        f"(confidence {result.confidence:.2f})"
    )
    assert 0.0 <= result.confidence <= 1.0


async def test_the_result_carries_a_probability_per_option(openai_key: str) -> None:
    """1.12's `ChoiceResult` is what the detector's `decision_timeout` acts on,
    and what a future Jev classifier would return calibrated. Assert the shape
    the detector reads, not just the winning label."""
    from pipecat.extensions.voicemail.voicemail_detector import VOICEMAIL_QUESTION

    results = await _classifier(openai_key).choice(
        "you've reached Dana, leave a message", {"voicemail": VOICEMAIL_QUESTION}
    )
    result = results["voicemail"]

    assert set(result.probabilities) == {"conversation", "voicemail"}
    assert result.probabilities["voicemail"] > result.probabilities["conversation"]
