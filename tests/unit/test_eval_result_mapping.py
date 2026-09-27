"""One pipecat result -> one `results` entry, and the run's verdict out of them.

Canned results throughout: no pipeline, no socket, no judge, no provider. What
is covered is the two mappers — separate functions on purpose, since handing one
a result from the other reports nonsense confidently — the confidence recorded
beside every verdict (#138), and `derive_status` one level up.

The rule under test throughout is that `errored` is not a kind of `failed`. An
iteration whose harness did not complete is neither a pass nor a fail and never
counts toward a rate: a judge outage reading as an agent regression is how a
suite loses its audience.
"""

from types import SimpleNamespace

import pytest

from tests.unit.eval_results import (
    expectation,
    failure,
    metric_score,
    parsed,
    said,
    script_result,
    simulation_result,
    turn_result,
)
from turncall.domain.enums import EvalRunStatus
from turncall.evals.result_mapping import (
    IterationOutcome,
    derive_status,
    errored_entry,
    map_script_result,
    map_simulation_result,
)

pytestmark = pytest.mark.unit


class TestDeriveStatus:
    def test_every_iteration_passing_passes_the_run(self) -> None:
        outcomes = [IterationOutcome(True, {}), IterationOutcome(True, {})]
        assert derive_status(outcomes) == (EvalRunStatus.PASSED, 2, 0)

    def test_one_failure_fails_the_run(self) -> None:
        """A scripted scenario that fails once has found something real."""
        outcomes = [IterationOutcome(True, {}), IterationOutcome(False, {})]
        assert derive_status(outcomes) == (EvalRunStatus.FAILED, 1, 1)

    def test_no_verdict_at_all_is_errored_not_failed(self) -> None:
        outcomes = [IterationOutcome(None, {}), IterationOutcome(None, {})]
        assert derive_status(outcomes) == (EvalRunStatus.ERRORED, 0, 0)

    def test_an_errored_iteration_counts_toward_neither_rate(self) -> None:
        """7/10 must mean seven of ten that actually ran."""
        outcomes = [
            IterationOutcome(True, {}),
            IterationOutcome(None, {}),
            IterationOutcome(False, {}),
        ]
        status, passed, failed = derive_status(outcomes)
        assert (status, passed, failed) == (EvalRunStatus.FAILED, 1, 1)
        assert passed + failed == 2, "the errored iteration must not be counted"


class TestMapScriptResult:
    def test_a_passing_result_maps_to_a_passed_entry(self) -> None:
        outcome = map_script_result(
            script_result(turns=[turn_result()]),
            iteration=1,
            parsed=parsed("hi there"),
        )
        assert outcome.passed is True
        assert outcome.entry["iteration"] == 1
        assert outcome.entry["duration_ms"] == 4120
        assert outcome.entry["turns"][0]["status"] == "passed"

    def test_the_failing_expectation_reason_survives(self) -> None:
        failed_assertion = failure()
        outcome = map_script_result(
            script_result(
                passed=False,
                failures=[failed_assertion],
                turns=[turn_result(status="failed", failures=[failed_assertion])],
            ),
            iteration=1,
            parsed=parsed("book me in"),
        )
        assert outcome.passed is False
        assert outcome.entry["failures"][0]["reason"] == (
            "expected transfer_call, got none"
        )
        assert outcome.entry["failures"][0]["kind"] == "missing_function_call"
        assert outcome.entry["turns"][0]["failures"][0]["turn_index"] == 2

    def test_a_skipped_result_is_neither_passed_nor_failed(self) -> None:
        """Pipecat draws this line itself and so do we."""
        outcome = map_script_result(
            script_result(passed=False, skipped="judge modality is text"),
            iteration=3,
            parsed=parsed(),
        )
        assert outcome.passed is None
        assert outcome.entry["skipped"] == "judge modality is text"
        assert derive_status([outcome])[0] is EvalRunStatus.ERRORED

    def test_the_transcript_interleaves_the_user_and_the_bot(self) -> None:
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0), turn_result(1)],
                events=[said("Hi, how can I help?"), said("Sure, booking that.")],
            ),
            iteration=1,
            parsed=parsed("hello", "book me in"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi, how can I help?"},
            {"role": "user", "content": "book me in"},
            {"role": "assistant", "content": "Sure, booking that."},
        ]

    def test_a_turn_that_never_ran_is_not_put_in_the_callers_mouth(self) -> None:
        """`stop_on_failure` is the default, so a failing run leaves later turns
        `not_run`. Rendering their scripted text showed the caller saying things
        the harness never sent — measured against a real run, not imagined."""
        outcome = map_script_result(
            script_result(
                passed=False,
                turns=[
                    turn_result(0),
                    turn_result(1, status="failed", expectations=[]),
                    turn_result(2, status="not_run", expectations=[]),
                ],
                events=[said("Hello!"), said("2+2 equals 4.")],
            ),
            iteration=1,
            parsed=parsed("Say hello.", "What is 2+2?", "Third turn never sent."),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "Say hello."},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "2+2 equals 4."},
        ]

    def test_a_failing_turn_still_records_what_the_agent_said(self) -> None:
        """A failing turn matches no expectation, so building the bot side from
        `expectation.matched` blanked the transcript exactly when it matters."""
        failed_assertion = failure()
        outcome = map_script_result(
            script_result(
                passed=False,
                failures=[failed_assertion],
                turns=[
                    turn_result(
                        0, status="failed", expectations=[], failures=[failed_assertion]
                    )
                ],
                events=[said("I have no idea what you mean.")],
            ),
            iteration=1,
            parsed=parsed("book me in"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "book me in"},
            {"role": "assistant", "content": "I have no idea what you mean."},
        ]

    def test_speech_beyond_one_reply_a_turn_is_kept_not_dropped(self) -> None:
        """One-per-turn is exact for text mode; losing the remainder would be
        the bug this replaced, so leftovers are appended."""
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[said("first"), said("and also this")],
            ),
            iteration=1,
            parsed=parsed("hello"),
        )
        assert [m["content"] for m in outcome.entry["transcript"]] == [
            "hello",
            "first",
            "and also this",
        ]

    def test_a_keypress_is_the_callers_turn_too(self) -> None:
        parsed = SimpleNamespace(turns=[SimpleNamespace(user=None, dtmf="123#")])
        outcome = map_script_result(
            script_result(turns=[turn_result(0)], events=[said("Got it.")]),
            iteration=1,
            parsed=parsed,
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "(DTMF keypad input: 123#)"},
            {"role": "assistant", "content": "Got it."},
        ]

    def test_a_non_speech_event_stays_out_of_the_transcript(self) -> None:
        """A function call is an assertion subject, not something anyone said."""
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[
                    {"type": "function_call", "name": "book_appointment", "at": 0.3}
                ],
            ),
            iteration=1,
            parsed=parsed("book me in"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "book me in"}
        ]


class TestMapSimulationResult:
    """#73. A simulation answers "did the conversation reach the right
    outcome", so its result is a goal verdict plus per-metric scores — a
    different shape from a scripted run's per-turn expectations."""

    def test_a_passing_run_carries_the_goal_verdict_and_its_reason(self) -> None:
        outcome = map_simulation_result(simulation_result(), iteration=1)
        assert outcome.passed is True
        assert outcome.entry["goal"] == {
            "succeeded": True,
            "reason": "the agent found the booking and read it back",
            # Absent, because this result reports none — see TestVerdictConfidence.
            "confidence": None,
        }
        assert outcome.entry["ended_by"] == "end_call"
        assert outcome.entry["persona_turns"] == 3
        assert outcome.entry["duration_ms"] == 18400

    def test_the_conversation_is_the_transcript(self) -> None:
        outcome = map_simulation_result(simulation_result(), iteration=1)
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "I lost my booking reference"},
            {"role": "assistant", "content": "I can find that for you."},
        ]

    def test_a_metric_below_its_minimum_fails_the_iteration(self) -> None:
        """Pipecat's own `passed` is the goal *and* every metric; a run whose
        goal was met but whose politeness collapsed is not a pass."""
        short = metric_score(passed=False, score=0.5, failure_kind="judge_no")
        outcome = map_simulation_result(simulation_result(metrics=[short]), iteration=1)
        assert outcome.passed is False
        assert outcome.entry["metrics"][0]["score"] == 0.5
        assert outcome.entry["metrics"][0]["failure_kind"] == "judge_no"
        assert outcome.entry["failures"] == [
            {
                "kind": "metric",
                "name": "politeness",
                "reason": "turn 2 was curt",
                # A metric has no single verdict of its own; its per-turn ones
                # each carry theirs under `metrics[].verdicts[]`.
                "confidence": None,
            }
        ]

    def test_a_reporting_metric_is_kept_even_though_it_fails_nothing(self) -> None:
        """A metric with no `min_score` never fails a run by design. Dropping it
        would leave the operator who asked to watch it with nothing to read."""
        watcher = metric_score("warmth", score=0.6, min_score=None)
        outcome = map_simulation_result(
            simulation_result(metrics=[watcher]), iteration=1
        )
        assert outcome.passed is True
        assert outcome.entry["metrics"][0]["min_score"] is None
        assert outcome.entry["metrics"][0]["score"] == 0.6

    def test_a_measured_metric_reports_its_value_not_a_verdict(self) -> None:
        latency = metric_score("latency", score=0.0, passed=False, value=4.7)
        outcome = map_simulation_result(
            simulation_result(metrics=[latency]), iteration=1
        )
        assert outcome.entry["metrics"][0]["value"] == 4.7
        assert outcome.entry["metrics"][0]["verdicts"] == []

    def test_a_missed_goal_fails_and_says_why(self) -> None:
        outcome = map_simulation_result(
            simulation_result(succeeded=False, reason="never found the booking"),
            iteration=1,
        )
        assert outcome.passed is False
        assert outcome.entry["failures"] == [
            {
                "kind": "goal",
                "name": "success",
                "reason": "never found the booking",
                "confidence": None,
            }
        ]

    def test_a_harness_error_is_errored_not_failed(self) -> None:
        """The conversation never finished, so it says nothing about the agent
        and must stay out of both counts."""
        outcome = map_simulation_result(
            simulation_result(error="persona LLM connection refused"), iteration=2
        )
        assert outcome.passed is None
        assert outcome.entry["error"] == "persona LLM connection refused"
        assert outcome.entry["iteration"] == 2

    def test_the_personas_own_claim_is_stored_and_marked_advisory(self) -> None:
        """Pipecat is explicit that the judge decides. Showing the two as
        equals teaches people to distrust the judge, so the label travels with
        the value rather than living in a UI that may not carry it."""
        outcome = map_simulation_result(
            simulation_result(
                succeeded=False,
                reason="the booking was never found",
                end_call={"success": True, "reason": "I think we sorted it"},
            ),
            iteration=1,
        )
        claim = outcome.entry["persona_claim"]
        assert claim == {
            "success": True,
            "reason": "I think we sorted it",
            "advisory": True,
        }
        # The judge disagreed, and the judge is what the verdict follows.
        assert outcome.passed is False

    def test_no_claim_when_the_persona_never_hung_up(self) -> None:
        outcome = map_simulation_result(
            simulation_result(end_call=None, ended_by="max_turns"), iteration=1
        )
        assert outcome.entry["persona_claim"] is None
        assert outcome.entry["ended_by"] == "max_turns"


def verdict(turn=1, *, passed=True, **over):
    return SimpleNamespace(
        turn=turn,
        passed=passed,
        verdict="yes" if passed else "no",
        reason="P(meets)=0.94",
        **over,
    )


class TestVerdictConfidence:
    """#138. How sure the judge was, recorded beside every verdict it gave.

    Both mappers, because they are separate functions: handing a simulation
    result to the scripted mapper reports nonsense confidently, which is why
    they were split. Nothing aggregates these and nothing branches on them —
    this slice records, and acting on a value is a later decision.

    Pipecat 1.12 carries a confidence on `JudgeVerdict` and then drops it
    building the result objects these mappers read, so a real run has none
    today; the fabricated results below are the shape it will have when it
    stops dropping it.
    """

    def test_a_scripted_expectation_carries_it(self) -> None:
        outcome = map_script_result(
            script_result(
                turns=[turn_result(expectations=[expectation(confidence=0.97)])]
            ),
            iteration=1,
            parsed=parsed("hi there"),
        )
        assert outcome.entry["turns"][0]["expectations"][0]["confidence"] == 0.97

    def test_a_scripted_failure_carries_it(self) -> None:
        failed_assertion = failure(confidence=0.62)
        outcome = map_script_result(
            script_result(
                passed=False,
                failures=[failed_assertion],
                turns=[turn_result(status="failed", failures=[failed_assertion])],
            ),
            iteration=1,
            parsed=parsed("book me in"),
        )
        assert outcome.entry["failures"][0]["confidence"] == 0.62
        assert outcome.entry["turns"][0]["failures"][0]["confidence"] == 0.62

    def test_a_simulations_goal_verdict_carries_it(self) -> None:
        outcome = map_simulation_result(simulation_result(confidence=0.88), iteration=1)
        assert outcome.entry["goal"]["confidence"] == 0.88

    def test_a_simulations_per_turn_verdict_carries_it(self) -> None:
        metric = metric_score(verdicts=[verdict(confidence=0.71)])
        outcome = map_simulation_result(
            simulation_result(metrics=[metric]), iteration=1
        )
        assert outcome.entry["metrics"][0]["verdicts"][0]["confidence"] == 0.71

    def test_a_judge_that_reports_none_is_absent_not_zero(self) -> None:
        """A missing measurement and a measurement of zero are different
        claims: zero is "certain of the opposite", the strongest verdict there
        is. `text_contains` and `function_call` build no judge at all."""
        outcome = map_script_result(
            script_result(
                passed=False,
                failures=[failure()],
                turns=[turn_result(status="failed", failures=[failure()])],
            ),
            iteration=1,
            parsed=parsed("book me in"),
        )
        assert outcome.entry["failures"][0]["confidence"] is None

        simulated = map_simulation_result(
            simulation_result(metrics=[metric_score(verdicts=[verdict()])]),
            iteration=1,
        )
        assert simulated.entry["goal"]["confidence"] is None
        assert simulated.entry["metrics"][0]["verdicts"][0]["confidence"] is None

    def test_a_simulations_failure_entry_carries_the_goal_verdicts_confidence(
        self,
    ) -> None:
        """The scripted mapper's failures carry one, so these do too: `_entry`'s
        rule is that a reader should not have to know which kind it holds to
        find a key. A metric's is None — its verdicts are per turn, and
        averaging them here is the aggregate #138 does not build."""
        short = metric_score(passed=False, score=0.5)
        outcome = map_simulation_result(
            simulation_result(succeeded=False, metrics=[short], confidence=0.55),
            iteration=1,
        )
        goal, metric = outcome.entry["failures"]
        assert goal["kind"] == "goal"
        assert goal["confidence"] == 0.55
        assert metric["kind"] == "metric"
        assert metric["confidence"] is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("0.9", id="a string that looks like one"),
            pytest.param(True, id="a bool, which is an int and would read 1.0"),
            pytest.param(1.5, id="above one"),
            pytest.param(-0.2, id="below zero"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
        ],
    )
    def test_only_a_real_probability_is_stored(self, value) -> None:
        """Everything pipecat's `float | None` could wrongly become is a worse
        lie than absence — `True` would read as certainty at the top of the
        scale — and none of it may raise, which would lose the whole run's
        results."""
        outcome = map_simulation_result(
            simulation_result(confidence=value), iteration=1
        )
        assert outcome.entry["goal"]["confidence"] is None

    def test_an_int_is_stored_as_a_float(self) -> None:
        """A judge certain of the verdict may report `1`, not `1.0`; the column
        is a probability either way."""
        outcome = map_simulation_result(simulation_result(confidence=1), iteration=1)
        assert outcome.entry["goal"]["confidence"] == 1.0
        assert isinstance(outcome.entry["goal"]["confidence"], float)

    def test_zero_is_kept_as_zero(self) -> None:
        """The other half of the same rule: a judge certain the reply was wrong
        reported 0.0, and storing that as absent would lose the clearest
        verdict in the set."""
        outcome = map_simulation_result(
            simulation_result(succeeded=False, confidence=0.0), iteration=1
        )
        assert outcome.entry["goal"]["confidence"] == 0.0

    def test_nothing_aggregates_it(self) -> None:
        """No run-level confidence, no average, no score column (#138): a
        scripted 1/1 and a simulation 7/10 are already one representation read
        twice."""
        outcome = map_simulation_result(simulation_result(confidence=0.88), iteration=1)
        assert "confidence" not in outcome.entry
        assert "score" not in outcome.entry


class TestSimulationPassRate:
    """A persona does not say the same thing twice, so one run is an anecdote.
    The run reports passed and failed against iterations — no score column: a
    scripted `1/1` and a simulation `7/10` are one representation read twice."""

    def test_counts_are_out_of_the_iterations_that_reached_a_verdict(self) -> None:
        outcomes = [
            map_simulation_result(simulation_result(), iteration=1),
            map_simulation_result(simulation_result(succeeded=False), iteration=2),
            map_simulation_result(simulation_result(), iteration=3),
            map_simulation_result(
                simulation_result(error="judge timed out"), iteration=4
            ),
        ]
        # Four conversations, three verdicts: the errored one counts toward
        # neither rate.
        assert derive_status(outcomes) == (EvalRunStatus.FAILED, 2, 1)

    def test_every_iteration_erroring_errors_the_run(self) -> None:
        outcomes = [
            map_simulation_result(simulation_result(error="boom"), iteration=i)
            for i in (1, 2)
        ]
        assert derive_status(outcomes) == (EvalRunStatus.ERRORED, 0, 0)


class TestErroredEntry:
    def test_it_carries_the_reason_and_no_verdict(self) -> None:
        outcome = errored_entry(2, "ConnectionRefusedError: nothing listening")
        assert outcome.passed is None
        assert outcome.entry["error"] == "ConnectionRefusedError: nothing listening"
        assert outcome.entry["iteration"] == 2


class TestAudioTranscript:
    """#72. In audio modality one reply arrives three ways: the LLM's text, the
    TTS's segments, and the harness's transcription of the audio. The judge
    reads the transcription, so that is the transcript — with the model's own
    text beside it, because when an audio run fails where a text run passed,
    the difference between those two is the whole explanation."""

    def test_the_judges_transcription_is_the_content(self) -> None:
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[
                    {"type": "llm_response", "text": "Your table is booked for two."},
                    {"type": "tts_response", "text": "Your table is booked"},
                    {"type": "tts_response", "text": " for two."},
                    {"type": "response", "text": "your table is book for to"},
                ],
            ),
            iteration=1,
            parsed=parsed("book me a table"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "book me a table"},
            {
                "role": "assistant",
                # what the judge heard
                "content": "your table is book for to",
                # what the agent actually said
                "text": "Your table is booked for two.",
            },
        ]

    def test_a_reply_is_one_entry_not_one_per_spoken_segment(self) -> None:
        """`tts_response` fires per segment, so counting it as speech gave one
        reply three transcript lines."""
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[
                    {"type": "tts_response", "text": "one"},
                    {"type": "tts_response", "text": "two"},
                    {"type": "tts_response", "text": "three"},
                    {"type": "response", "text": "one two three"},
                ],
            ),
            iteration=1,
            parsed=parsed("hello"),
        )
        assistant = [t for t in outcome.entry["transcript"] if t["role"] == "assistant"]
        assert assistant == [{"role": "assistant", "content": "one two three"}]

    def test_text_modality_is_unchanged(self) -> None:
        """No transcription event, so the LLM text is the content and there is
        no second field to carry."""
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[{"type": "llm_response", "text": "Hi there."}],
            ),
            iteration=1,
            parsed=parsed("hello"),
        )
        assert outcome.entry["transcript"][1] == {
            "role": "assistant",
            "content": "Hi there.",
        }

    def test_an_identical_transcription_carries_no_duplicate(self) -> None:
        outcome = map_script_result(
            script_result(
                turns=[turn_result(0)],
                events=[
                    {"type": "llm_response", "text": "Hi there."},
                    {"type": "response", "text": "Hi there."},
                ],
            ),
            iteration=1,
            parsed=parsed("hello"),
        )
        assert outcome.entry["transcript"][1] == {
            "role": "assistant",
            "content": "Hi there.",
        }
