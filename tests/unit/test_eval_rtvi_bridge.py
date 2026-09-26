"""The eval RTVI bridge is two halves, and half of it is silent (§9.7).

Pipecat's eval harness is an RTVI *client*. The server half is an
`RTVIProcessor` in the bot's pipeline plus an `RTVIObserver` on the bot's
task. Neither was ever built, so every iteration died waiting for `bot-ready`
and scored `errored` — which is deliberately kept out of every pass/fail rate,
so a total outage reported as "no signal" rather than as a red build.

That is also why this guard is structural and hermetic rather than a live run.
The live suite that should have caught it is credential-gated, and it happens
to contain a decoy: `test_a_first_message_is_invisible_to_a_text_mode_eval`
asserted `events_seen == []`, which is exactly what a *dead* bridge produces
too. A pinned coverage hole and a total outage looked identical from there.

Dropping either half leaves the same symptom as dropping both, so both are
asserted here, in the place a reader of one will find the other.
"""

import ast
import contextlib
from pathlib import Path

import pytest

import turncall

_CALL_SESSION = Path(turncall.__file__).parent / "orchestrator" / "call_session.py"


def _captured_processors(mocker, *, is_eval: bool, pipeline_mode: str) -> list:
    """The processor list `create_pipeline` hands to `Pipeline`.

    Patching `Pipeline` to capture rather than asserting on the built object:
    the same trick `test_idle_caller.py` uses, and it keeps the test away from
    every service the pipeline would otherwise want credentials for.
    """
    from turncall.domain.models import AgentConfig
    from turncall.orchestrator import pipeline_factory

    captured: list = []

    def _capture(processors):
        captured.extend(processors)
        return mocker.MagicMock()

    call_context = mocker.MagicMock()
    call_context.is_eval = is_eval

    mocker.patch.object(pipeline_factory, "Pipeline", _capture)
    # Assembly is all this needs; the build may well stop after it.
    with contextlib.suppress(Exception):
        pipeline_factory.create_pipeline(
            config=AgentConfig(pipeline_mode=pipeline_mode),
            transport=mocker.MagicMock(),
            call_context=call_context,
            openai_api_key="test-key",
            pipecat_settings=mocker.MagicMock(),
        )
    return captured


def _rtvi_processors(processors: list) -> list:
    from pipecat.processors.frameworks.rtvi import RTVIProcessor

    return [p for p in processors if isinstance(p, RTVIProcessor)]


@pytest.mark.unit
@pytest.mark.parametrize("pipeline_mode", ["cascade", "s2s"])
class TestTheProcessorHalf:
    def test_an_eval_pipeline_has_exactly_one_rtvi_processor(
        self, mocker, pipeline_mode: str
    ) -> None:
        processors = _captured_processors(
            mocker, is_eval=True, pipeline_mode=pipeline_mode
        )

        assert len(_rtvi_processors(processors)) == 1, (
            f"{pipeline_mode}: no RTVIProcessor in an eval pipeline — nothing "
            "sends `bot-ready`, so every iteration dies at handshake() after "
            "10s and scores `errored`, which is kept out of every rate"
        )

    def test_it_sits_directly_after_the_transport_input(
        self, mocker, pipeline_mode: str
    ) -> None:
        """Position is load-bearing twice over.

        It has to be downstream of `transport.input()` to see the
        `InputTransportMessageFrame`s the eval serializer produces, and it has
        to be in the list from the start: `client-ready` arrives as soon as the
        harness connects, so a processor added from an `on_client_connected`
        handler is already too late.
        """
        processors = _captured_processors(
            mocker, is_eval=True, pipeline_mode=pipeline_mode
        )

        from pipecat.processors.frameworks.rtvi import RTVIProcessor

        assert len(processors) > 1, (
            f"{pipeline_mode}: pipeline assembly produced nothing"
        )
        assert isinstance(processors[1], RTVIProcessor), (
            f"{pipeline_mode}: the RTVIProcessor is not immediately after "
            f"transport.input() (found {type(processors[1]).__name__})"
        )

    def test_a_live_call_gets_none(self, mocker, pipeline_mode: str) -> None:
        processors = _captured_processors(
            mocker, is_eval=False, pipeline_mode=pipeline_mode
        )

        assert _rtvi_processors(processors) == [], (
            f"{pipeline_mode}: a live call gained an RTVIProcessor — the bridge "
            "is meant to be eval-path only"
        )


@pytest.mark.unit
class TestTheObserverHalf:
    def test_an_rtvi_processor_in_the_pipeline_yields_its_observer(self) -> None:
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor

        from turncall.orchestrator.pipeline_factory import eval_rtvi_observers

        observers = eval_rtvi_observers(Pipeline([RTVIProcessor()]))

        assert len(observers) == 1
        assert isinstance(observers[0], RTVIObserver), (
            "without the observer, no asserted event is ever emitted — "
            "`llm_response` and `function_call` come only from RTVIObserver, "
            "so events_seen stays empty even where the agent worked perfectly"
        )

    def test_a_live_pipeline_yields_nothing(self) -> None:
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
        )

        from turncall.orchestrator.pipeline_factory import eval_rtvi_observers

        pair = LLMContextAggregatorPair(LLMContext())

        assert eval_rtvi_observers(Pipeline([pair.user(), pair.assistant()])) == []

    def test_call_session_actually_asks_for_them(self) -> None:
        """The half that a unit test cannot run, guarded the way
        `test_kb_pipeline_wiring.py` guards its own wiring.

        `start()` builds a PipelineWorker with a live transport and runner, so
        AST is what is left. It is enough for the failure this exists for: the
        observer being built nowhere at all.
        """
        tree = ast.parse(_CALL_SESSION.read_text())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "eval_rtvi_observers" in called, (
            "call_session.py never calls eval_rtvi_observers — the processor "
            "half alone still fails the handshake configuration and emits no "
            "asserted events"
        )
