"""The voicemail detector takes a classifier now, and the custom prompt is
additive rather than a replacement.

pipecat 1.12 rewrote `VoicemailDetector` from a `ParallelPipeline` with its own
LLM into a single `FrameProcessor` that asks a `Classifier`. `llm=` and
`custom_system_prompt=` still work through a shim, which is removed in 2.0.0.

The trap this guards is the second parameter, not the first. `LLMClassifier`'s
`instructions` **replace** its defaults rather than extending them — and the
defaults are what ask for the JSON object the classifier parses. An agent's
`custom_system_prompt` passed straight through would therefore leave every
verdict unreadable, which reads at runtime as "classification failed" in a log
line and a call that never detects voicemail at all.
"""

import pytest


def _detector(mocker, *, custom_system_prompt: str | None):
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator import pipeline_factory

    built: list = []

    def _capture(processors):
        built.extend(processors)
        return mocker.MagicMock()

    call_context = mocker.MagicMock()
    call_context.is_eval = False

    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    config = AgentConfig(
        voicemail_detection={
            "enabled": True,
            "custom_system_prompt": custom_system_prompt,
        }
    )
    pipeline_factory.create_pipeline(
        config=config,
        transport=mocker.MagicMock(),
        call_context=call_context,
        openai_api_key="test-key",
        pipecat_settings=mocker.MagicMock(),
    )

    from pipecat.extensions.voicemail.voicemail_detector import VoicemailDetector

    # `detector()` returns the VoicemailDetector itself; `gate()` returns the
    # separate TTSGate. Both are in the list, only one is this.
    return next((p for p in built if isinstance(p, VoicemailDetector)), None)


@pytest.mark.unit
class TestTheDetectorIsBuiltOnAClassifier:
    def test_it_is_given_a_classifier_not_the_deprecated_llm(self, mocker) -> None:
        from pipecat.classifiers.llm.classifier import LLMClassifier

        detector = _detector(mocker, custom_system_prompt=None)

        assert detector is not None, "no VoicemailDetector in the pipeline"
        assert isinstance(detector._classifier, LLMClassifier), (
            "the detector was built without a classifier — the `llm=` shim is "
            "removed in pipecat 2.0.0"
        )

    def test_a_custom_prompt_is_prepended_to_the_default_instructions(
        self, mocker
    ) -> None:
        from pipecat.classifiers.llm.classifier import DEFAULT_INSTRUCTIONS

        prompt = "This line is answered by a medical practice."
        detector = _detector(mocker, custom_system_prompt=prompt)

        instructions = detector._classifier._instructions
        assert prompt in instructions, "the agent's custom prompt was dropped"
        assert instructions.endswith(DEFAULT_INSTRUCTIONS), (
            "the classifier's own instructions were replaced rather than "
            "extended — they are what ask for the JSON object it parses, so "
            "every verdict would come back unreadable"
        )

    def test_no_custom_prompt_leaves_the_defaults_alone(self, mocker) -> None:
        from pipecat.classifiers.llm.classifier import DEFAULT_INSTRUCTIONS

        detector = _detector(mocker, custom_system_prompt=None)

        assert detector._classifier._instructions == DEFAULT_INSTRUCTIONS
