"""The eval runner's orchestration, with canned pipecat results.

No pipeline and no socket: everything that needs those lives behind the
injected `execute`, so what is covered here is target resolution, the iteration
loop, status derivation, the counts and the result mapping.

The rule under test throughout is that `errored` is not a kind of `failed`. An
iteration whose harness did not complete is neither a pass nor a fail and never
counts toward a rate — a judge outage reading as an agent regression is how a
suite loses its audience.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.domain.enums import EvalKind, EvalRunStatus
from turncall.domain.models import AgentConfig
from turncall.evals.runner import (
    IterationOutcome,
    ResolvedTarget,
    TargetError,
    derive_status,
    errored_entry,
    harness_config,
    map_script_result,
    map_simulation_result,
    resolve_target,
    resolved_scenario_snapshot,
    tool_policy_of,
)

pytestmark = pytest.mark.unit


def _failure(**over):
    return SimpleNamespace(
        turn_index=2,
        expectation_index=0,
        event_name="function_call",
        kind="missing_function_call",
        reason="expected transfer_call, got none",
        **over,
    )


def _expectation(matched="Hello there", event_name="llm_response", passed=True):
    return SimpleNamespace(
        expectation_index=0,
        event_name=event_name,
        passed=passed,
        matched=matched,
    )


def _script_result(
    *, passed=True, skipped=None, turns=None, failures=None, events=None
):
    return SimpleNamespace(
        scenario_name="greets-the-caller",
        passed=passed,
        skipped=skipped,
        duration_ms=4120,
        failures=failures or [],
        turns=turns if turns is not None else [],
        events_seen=events or [],
        debug_log=[],
    )


def _said(text):
    """A bot-speech event as the harness records it.

    No timestamp: attribution is by order now. Windowing by the running total
    of `duration_ms` was wrong — `at` starts at the harness's connect, which no
    turn's duration accounts for.
    """
    return {"type": "llm_response", "text": text}


def _turn_result(index=0, status="passed", expectations=None, failures=None):
    return SimpleNamespace(
        turn_index=index,
        status=status,
        duration_ms=900,
        expectations=expectations if expectations is not None else [_expectation()],
        failures=failures or [],
    )


def _metric_score(name="politeness", *, score=1.0, passed=True, min_score=1.0, **over):
    return SimpleNamespace(
        name=name,
        score=score,
        passed=passed,
        reason="every turn passed" if passed else "turn 2 was curt",
        min_score=min_score,
        verdicts=over.pop("verdicts", []),
        value=over.pop("value", None),
        failure_kind=over.pop("failure_kind", None),
        **over,
    )


def _simulation_result(
    *, succeeded=True, error=None, metrics=None, messages=None, **over
):
    """What pipecat's `EvalSimulationResult` gives us. `passed` is its own
    property: the goal met *and* no metric short."""
    metrics = [] if metrics is None else metrics
    return SimpleNamespace(
        simulation_name="recovers-the-booking",
        succeeded=succeeded,
        reason=over.pop("reason", "the agent found the booking and read it back"),
        error=error,
        metrics=metrics,
        messages=messages
        if messages is not None
        else [
            {"role": "user", "content": "I lost my booking reference"},
            {"role": "assistant", "content": "I can find that for you."},
        ],
        turns=over.pop("turns", 3),
        ended_by=over.pop("ended_by", "end_call"),
        end_call=over.pop("end_call", {"success": True, "reason": "got it"}),
        duration_ms=over.pop("duration_ms", 18400),
        events_seen=[],
        debug_log=[],
        passed=error is None and succeeded and all(m.passed for m in metrics),
        **over,
    )


def _parsed(*user_turns):
    return SimpleNamespace(
        turns=[SimpleNamespace(user=u, dtmf=None) for u in user_turns]
    )


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
            _script_result(turns=[_turn_result()]),
            iteration=1,
            parsed=_parsed("hi there"),
        )
        assert outcome.passed is True
        assert outcome.entry["iteration"] == 1
        assert outcome.entry["duration_ms"] == 4120
        assert outcome.entry["turns"][0]["status"] == "passed"

    def test_the_failing_expectation_reason_survives(self) -> None:
        failure = _failure()
        outcome = map_script_result(
            _script_result(
                passed=False,
                failures=[failure],
                turns=[_turn_result(status="failed", failures=[failure])],
            ),
            iteration=1,
            parsed=_parsed("book me in"),
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
            _script_result(passed=False, skipped="judge modality is text"),
            iteration=3,
            parsed=_parsed(),
        )
        assert outcome.passed is None
        assert outcome.entry["skipped"] == "judge modality is text"
        assert derive_status([outcome])[0] is EvalRunStatus.ERRORED

    def test_the_transcript_interleaves_the_user_and_the_bot(self) -> None:
        outcome = map_script_result(
            _script_result(
                turns=[_turn_result(0), _turn_result(1)],
                events=[_said("Hi, how can I help?"), _said("Sure, booking that.")],
            ),
            iteration=1,
            parsed=_parsed("hello", "book me in"),
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
            _script_result(
                passed=False,
                turns=[
                    _turn_result(0),
                    _turn_result(1, status="failed", expectations=[]),
                    _turn_result(2, status="not_run", expectations=[]),
                ],
                events=[_said("Hello!"), _said("2+2 equals 4.")],
            ),
            iteration=1,
            parsed=_parsed("Say hello.", "What is 2+2?", "Third turn never sent."),
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
        failure = _failure()
        outcome = map_script_result(
            _script_result(
                passed=False,
                failures=[failure],
                turns=[
                    _turn_result(
                        0, status="failed", expectations=[], failures=[failure]
                    )
                ],
                events=[_said("I have no idea what you mean.")],
            ),
            iteration=1,
            parsed=_parsed("book me in"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "book me in"},
            {"role": "assistant", "content": "I have no idea what you mean."},
        ]

    def test_speech_beyond_one_reply_a_turn_is_kept_not_dropped(self) -> None:
        """One-per-turn is exact for text mode; losing the remainder would be
        the bug this replaced, so leftovers are appended."""
        outcome = map_script_result(
            _script_result(
                turns=[_turn_result(0)],
                events=[_said("first"), _said("and also this")],
            ),
            iteration=1,
            parsed=_parsed("hello"),
        )
        assert [m["content"] for m in outcome.entry["transcript"]] == [
            "hello",
            "first",
            "and also this",
        ]

    def test_a_keypress_is_the_callers_turn_too(self) -> None:
        parsed = SimpleNamespace(turns=[SimpleNamespace(user=None, dtmf="123#")])
        outcome = map_script_result(
            _script_result(turns=[_turn_result(0)], events=[_said("Got it.")]),
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
            _script_result(
                turns=[_turn_result(0)],
                events=[
                    {"type": "function_call", "name": "book_appointment", "at": 0.3}
                ],
            ),
            iteration=1,
            parsed=_parsed("book me in"),
        )
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "book me in"}
        ]


class TestMapSimulationResult:
    """#73. A simulation answers "did the conversation reach the right
    outcome", so its result is a goal verdict plus per-metric scores — a
    different shape from a scripted run's per-turn expectations."""

    def test_a_passing_run_carries_the_goal_verdict_and_its_reason(self) -> None:
        outcome = map_simulation_result(_simulation_result(), iteration=1)
        assert outcome.passed is True
        assert outcome.entry["goal"] == {
            "succeeded": True,
            "reason": "the agent found the booking and read it back",
        }
        assert outcome.entry["ended_by"] == "end_call"
        assert outcome.entry["persona_turns"] == 3
        assert outcome.entry["duration_ms"] == 18400

    def test_the_conversation_is_the_transcript(self) -> None:
        outcome = map_simulation_result(_simulation_result(), iteration=1)
        assert outcome.entry["transcript"] == [
            {"role": "user", "content": "I lost my booking reference"},
            {"role": "assistant", "content": "I can find that for you."},
        ]

    def test_a_metric_below_its_minimum_fails_the_iteration(self) -> None:
        """Pipecat's own `passed` is the goal *and* every metric; a run whose
        goal was met but whose politeness collapsed is not a pass."""
        short = _metric_score(passed=False, score=0.5, failure_kind="judge_no")
        outcome = map_simulation_result(
            _simulation_result(metrics=[short]), iteration=1
        )
        assert outcome.passed is False
        assert outcome.entry["metrics"][0]["score"] == 0.5
        assert outcome.entry["metrics"][0]["failure_kind"] == "judge_no"
        assert outcome.entry["failures"] == [
            {"kind": "metric", "name": "politeness", "reason": "turn 2 was curt"}
        ]

    def test_a_reporting_metric_is_kept_even_though_it_fails_nothing(self) -> None:
        """A metric with no `min_score` never fails a run by design. Dropping it
        would leave the operator who asked to watch it with nothing to read."""
        watcher = _metric_score("warmth", score=0.6, min_score=None)
        outcome = map_simulation_result(
            _simulation_result(metrics=[watcher]), iteration=1
        )
        assert outcome.passed is True
        assert outcome.entry["metrics"][0]["min_score"] is None
        assert outcome.entry["metrics"][0]["score"] == 0.6

    def test_a_measured_metric_reports_its_value_not_a_verdict(self) -> None:
        latency = _metric_score("latency", score=0.0, passed=False, value=4.7)
        outcome = map_simulation_result(
            _simulation_result(metrics=[latency]), iteration=1
        )
        assert outcome.entry["metrics"][0]["value"] == 4.7
        assert outcome.entry["metrics"][0]["verdicts"] == []

    def test_a_missed_goal_fails_and_says_why(self) -> None:
        outcome = map_simulation_result(
            _simulation_result(succeeded=False, reason="never found the booking"),
            iteration=1,
        )
        assert outcome.passed is False
        assert outcome.entry["failures"] == [
            {"kind": "goal", "name": "success", "reason": "never found the booking"}
        ]

    def test_a_harness_error_is_errored_not_failed(self) -> None:
        """The conversation never finished, so it says nothing about the agent
        and must stay out of both counts."""
        outcome = map_simulation_result(
            _simulation_result(error="persona LLM connection refused"), iteration=2
        )
        assert outcome.passed is None
        assert outcome.entry["error"] == "persona LLM connection refused"
        assert outcome.entry["iteration"] == 2

    def test_the_personas_own_claim_is_stored_and_marked_advisory(self) -> None:
        """Pipecat is explicit that the judge decides. Showing the two as
        equals teaches people to distrust the judge, so the label travels with
        the value rather than living in a UI that may not carry it."""
        outcome = map_simulation_result(
            _simulation_result(
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
            _simulation_result(end_call=None, ended_by="max_turns"), iteration=1
        )
        assert outcome.entry["persona_claim"] is None
        assert outcome.entry["ended_by"] == "max_turns"


class TestSimulationPassRate:
    """A persona does not say the same thing twice, so one run is an anecdote.
    The run reports passed and failed against iterations — no score column: a
    scripted `1/1` and a simulation `7/10` are one representation read twice."""

    def test_counts_are_out_of_the_iterations_that_reached_a_verdict(self) -> None:
        outcomes = [
            map_simulation_result(_simulation_result(), iteration=1),
            map_simulation_result(_simulation_result(succeeded=False), iteration=2),
            map_simulation_result(_simulation_result(), iteration=3),
            map_simulation_result(
                _simulation_result(error="judge timed out"), iteration=4
            ),
        ]
        # Four conversations, three verdicts: the errored one counts toward
        # neither rate.
        assert derive_status(outcomes) == (EvalRunStatus.FAILED, 2, 1)

    def test_every_iteration_erroring_errors_the_run(self) -> None:
        outcomes = [
            map_simulation_result(_simulation_result(error="boom"), iteration=i)
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
            _script_result(
                turns=[_turn_result(0)],
                events=[
                    {"type": "llm_response", "text": "Your table is booked for two."},
                    {"type": "tts_response", "text": "Your table is booked"},
                    {"type": "tts_response", "text": " for two."},
                    {"type": "response", "text": "your table is book for to"},
                ],
            ),
            iteration=1,
            parsed=_parsed("book me a table"),
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
            _script_result(
                turns=[_turn_result(0)],
                events=[
                    {"type": "tts_response", "text": "one"},
                    {"type": "tts_response", "text": "two"},
                    {"type": "tts_response", "text": "three"},
                    {"type": "response", "text": "one two three"},
                ],
            ),
            iteration=1,
            parsed=_parsed("hello"),
        )
        assistant = [t for t in outcome.entry["transcript"] if t["role"] == "assistant"]
        assert assistant == [{"role": "assistant", "content": "one two three"}]

    def test_text_modality_is_unchanged(self) -> None:
        """No transcription event, so the LLM text is the content and there is
        no second field to carry."""
        outcome = map_script_result(
            _script_result(
                turns=[_turn_result(0)],
                events=[{"type": "llm_response", "text": "Hi there."}],
            ),
            iteration=1,
            parsed=_parsed("hello"),
        )
        assert outcome.entry["transcript"][1] == {
            "role": "assistant",
            "content": "Hi there.",
        }

    def test_an_identical_transcription_carries_no_duplicate(self) -> None:
        outcome = map_script_result(
            _script_result(
                turns=[_turn_result(0)],
                events=[
                    {"type": "llm_response", "text": "Hi there."},
                    {"type": "response", "text": "Hi there."},
                ],
            ),
            iteration=1,
            parsed=_parsed("hello"),
        )
        assert outcome.entry["transcript"][1] == {
            "role": "assistant",
            "content": "Hi there.",
        }


class TestToolPolicyResolution:
    """One place decides the fail-closed default — deciding it twice is how the
    safe value stops being the default (#71)."""

    def test_live_has_to_be_named_exactly(self) -> None:
        from turncall.domain.enums import EvalToolPolicy

        assert EvalToolPolicy.resolve("live") is EvalToolPolicy.LIVE
        assert EvalToolPolicy.resolve(EvalToolPolicy.LIVE) is EvalToolPolicy.LIVE

    def test_anything_else_fails_closed(self) -> None:
        """Including a value a future version wrote: a stored row must never
        make this raise inside a worker, and must never become `live` by
        accident."""
        from turncall.domain.enums import EvalToolPolicy

        for value in (None, "", "mock_only", "LIVE", "whatever", 7, {"a": 1}):
            assert EvalToolPolicy.resolve(value) is EvalToolPolicy.MOCK_ONLY

    def test_the_run_reads_its_mocks_off_the_snapshot(self) -> None:
        """The scenario row can be edited while the run sits queued; a run is
        what it was queued as."""
        from turncall.evals.runner import tool_policy_of

        mocks, live = tool_policy_of(
            {"tool_mocks": {"book": {"ok": True}}, "tool_policy": "live"}
        )
        assert (mocks, live) == ({"book": {"ok": True}}, True)

    def test_a_snapshot_missing_both_is_mock_only_with_no_mocks(self) -> None:
        assert tool_policy_of({}) == ({}, False)


class TestSnapshots:
    def test_the_scenario_snapshot_carries_the_mocks_and_the_policy(self) -> None:
        """Mocks are part of what the test means, so a run that does not record
        them cannot be compared with the next one."""
        snapshot = resolved_scenario_snapshot(
            definition={"turns": []},
            schema_version="pipecat-1.11",
            tool_mocks={"book": {"ok": True}},
            tool_policy="mock_only",
        )
        assert snapshot == {
            "definition": {"turns": []},
            "schema_version": "pipecat-1.11",
            "tool_mocks": {"book": {"ok": True}},
            "tool_policy": "mock_only",
            "tool_policy_enforced": True,
        }

    def test_the_snapshot_says_whether_the_policy_was_enforced(self) -> None:
        """Runs written before #71 say False: their `mock_only` was a column
        default nothing honoured, and a reader comparing across the slice has
        to be able to tell the two apart."""
        snapshot = resolved_scenario_snapshot(
            definition={},
            schema_version="pipecat-1.11",
            tool_mocks={},
            tool_policy="mock_only",
        )
        assert snapshot["tool_policy_enforced"] is True

    def test_the_harness_snapshot_names_the_judge_and_pipecat(self) -> None:
        """The judge decides the verdict; a silent provider-side model update
        moves the whole baseline, so the run has to say which one answered."""
        parsed = SimpleNamespace(
            judge={"service": "openai", "model": "gpt-4o-2024-11-20"}
        )
        config = harness_config(parsed)
        assert config["judge_model"] == "gpt-4o-2024-11-20"
        assert config["judge_service"] == "openai"
        assert config["judge_used"] is False, "no eval: assertion asks it anything"
        assert config["pipecat_version"].startswith("1.")
        assert config["schema_version"]

    def test_the_judge_snapshot_records_pipecats_default_too(self) -> None:
        """Pipecat fills `judge.eval:` with ollama/gemma when a scenario names
        nothing — which is not the judge most people assume they are running."""
        from turncall.domain.enums import EvalModality
        from turncall.evals.scenario import parse, with_modality

        parsed = parse(
            with_modality({"turns": [{"user": "hi", "expect": []}]}, EvalModality.TEXT),
            name="defaulted",
        )
        config = harness_config(parsed)
        assert config["judge_service"], "the run must name whatever judge ran"
        assert config["judge_model"]


class TestResolveTarget:
    async def test_an_agent_target_resolves_to_its_config(self) -> None:
        agent_id = project_id = uuid4()
        agent = SimpleNamespace(
            id=agent_id, config_blob={"system_prompt": "You are a receptionist."}
        )
        with patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            AsyncMock(return_value=agent),
        ):
            resolved = await resolve_target(
                AsyncMock(),
                project_id=project_id,
                target={"type": "agent", "agent_id": str(agent_id)},
            )
        assert resolved.agent_id == agent_id
        assert resolved.config.system_prompt == "You are a receptionist."
        assert resolved.config_blob == {"system_prompt": "You are a receptionist."}

    async def test_a_missing_agent_is_a_target_error(self) -> None:
        with patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(TargetError, match="not found"):
                await resolve_target(
                    AsyncMock(),
                    project_id=uuid4(),
                    target={"type": "agent", "agent_id": str(uuid4())},
                )

    async def test_an_unsupported_target_type_is_rejected(self) -> None:
        """Inline and latest-published are a later slice; a run must not be
        accepted and then silently do the wrong thing."""
        with pytest.raises(TargetError, match="unsupported target type"):
            await resolve_target(
                AsyncMock(), project_id=uuid4(), target={"type": "inline", "agent": {}}
            )


class TestExecuteRun:
    """The loop, end to end, against a fake repository and a fake harness."""

    @staticmethod
    def _run_row(iterations=1, status="queued"):
        return SimpleNamespace(
            id=uuid4(),
            project_id=uuid4(),
            status=status,
            modality="text",
            kind="script",
            iterations=iterations,
            scenario_name="greets-the-caller",
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={
                "definition": {
                    "turns": [
                        {
                            "user": "hello",
                            "expect": [
                                {"event": "llm_response", "text_contains": "hi"}
                            ],
                        }
                    ]
                }
            },
        )

    @staticmethod
    def _session_factory():
        session = AsyncMock()

        class _CM:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_):
                return False

        return (lambda: _CM()), session

    async def _execute(self, run_row, execute, *, finish=None, start=None):
        from turncall.evals import runner as runner_mod

        factory, _session = self._session_factory()
        finish = finish or AsyncMock()
        start = start or AsyncMock()
        target = ResolvedTarget(
            project_id=run_row.project_id,
            config=AgentConfig(),
            config_blob={"system_prompt": "hi"},
            agent_id=uuid4(),
        )
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run_row),
            ),
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
        ):
            await runner_mod.execute_run(
                run_row.id,
                session_factory=factory,
                settings=SimpleNamespace(),
                execute=execute,
            )
        return finish, start

    async def test_a_passing_run_records_the_counts_and_the_snapshots(self) -> None:
        execute = AsyncMock(return_value=_script_result(turns=[_turn_result()]))
        run = self._run_row(iterations=3)
        finish, start = await self._execute(run, execute)

        assert execute.await_count == 3, "one execution per iteration"
        assert start.await_args.kwargs["resolved_config"] == {"system_prompt": "hi"}
        assert start.await_args.kwargs["harness_config"]["pipecat_version"]
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (3, 0)
        assert len(kwargs["results"]) == 3

    async def test_one_failing_iteration_fails_the_run(self) -> None:
        results = [
            _script_result(turns=[_turn_result()]),
            _script_result(
                passed=False,
                failures=[_failure()],
                turns=[_turn_result(status="failed", failures=[_failure()])],
            ),
        ]
        execute = AsyncMock(side_effect=results)
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.FAILED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (1, 1)

    async def test_a_harness_that_raises_errors_rather_than_fails(self) -> None:
        execute = AsyncMock(side_effect=ConnectionRefusedError("nothing listening"))
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert "nothing listening" in kwargs["error"]

    async def test_the_scenarios_mocks_and_policy_reach_the_iteration(self) -> None:
        """#71: the mocks are the scenario's, so every iteration gets the same
        ones — but a fresh recorder, since what was called belongs to the
        iteration."""
        seen = []

        async def execute(**kwargs):
            seen.append(kwargs["tool_mocks"])
            return _script_result(turns=[_turn_result()])

        run = self._run_row(iterations=2)
        run.resolved_scenario["tool_mocks"] = {"book": {"ok": True}}
        run.resolved_scenario["tool_policy"] = "mock_only"
        await self._execute(run, execute)

        assert [m.responses for m in seen] == [{"book": {"ok": True}}] * 2
        assert [m.live for m in seen] == [False, False]
        assert seen[0] is not seen[1], "a shared recorder would pool the calls"

    async def test_an_unmocked_tool_errors_the_run_naming_it(self) -> None:
        """Fail closed. `errored`, never `failed`: nothing ran, so it says
        nothing about the agent and must stay out of every rate."""

        async def execute(**kwargs):
            kwargs["tool_mocks"].refused.append("book_appointment")
            kwargs["tool_mocks"].record(
                "book_appointment", {}, '{"error": "..."}', outcome="refused"
            )
            return _script_result(turns=[_turn_result()])

        finish, _ = await self._execute(self._run_row(), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert kwargs["error"] == "unmocked tool: book_appointment"
        entry = kwargs["results"][0]
        assert entry["tool_calls"][0]["tool_name"] == "book_appointment"

    async def test_a_mock_that_can_never_fire_is_called_out(self) -> None:
        """An eval connects no MCP servers, so an MCP tool is never advertised
        and a mock keyed to one means nothing — as does a typo. Silence is the
        worst outcome: the mock reads as a tool that was simply never called."""
        from turncall.evals import runner as runner_mod

        run = self._run_row()
        run.resolved_scenario["tool_mocks"] = {"searchCrm": {"hits": []}}
        with patch.object(runner_mod.logger, "warning") as warn:
            await self._execute(
                run, AsyncMock(return_value=_script_result(turns=[_turn_result()]))
            )
        assert any(
            call.args and call.args[0] == "eval_mock_matches_no_tool"
            for call in warn.call_args_list
        ), "a mock naming nothing the agent has must be reported"

    async def test_a_refusal_condemns_the_run_even_if_others_passed(self) -> None:
        """The run, not the iteration. A forgotten mock on a ten-iteration run
        used to report PASSED because the other nine never reached the tool —
        precisely the accident the policy exists to prevent. The run also stops
        there: the rest would hit the same missing mock."""
        calls = {"n": 0}

        async def execute(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                kwargs["tool_mocks"].refused.append("book_appointment")
            return _script_result(turns=[_turn_result()])

        finish, _ = await self._execute(self._run_row(iterations=5), execute)
        kwargs = finish.await_args.kwargs
        assert calls["n"] == 2, "the remaining iterations are not paid for"
        assert kwargs["status"] is EvalRunStatus.ERRORED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (0, 0)
        assert kwargs["error"] == "unmocked tool: book_appointment"
        # What did happen is still readable per iteration.
        assert kwargs["results"][0]["passed"] is True

    async def test_a_runs_results_carry_what_the_tools_did(self) -> None:
        """An eval has no `calls` row, so the run entry is the only tool record
        there is — and a mocked call has to be distinguishable from a real one."""

        async def execute(**kwargs):
            kwargs["tool_mocks"].record("book", {}, '{"ok": true}', outcome="mocked")
            return _script_result(turns=[_turn_result()])

        finish, _ = await self._execute(self._run_row(), execute)
        entry = finish.await_args.kwargs["results"][0]
        assert entry["tool_calls"] == [
            {
                "tool_name": "book",
                "arguments": {},
                "result": '{"ok": true}',
                "outcome": "mocked",
            }
        ]

    async def test_one_bad_iteration_does_not_stop_the_rest(self) -> None:
        execute = AsyncMock(
            side_effect=[
                TimeoutError("judge timed out"),
                _script_result(turns=[_turn_result()]),
            ]
        )
        finish, _ = await self._execute(self._run_row(iterations=2), execute)
        kwargs = finish.await_args.kwargs
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert (kwargs["passed_count"], kwargs["failed_count"]) == (1, 0)
        assert kwargs["results"][0]["error"].startswith("TimeoutError")

    async def test_an_unresolvable_target_errors_before_any_iteration(self) -> None:
        from turncall.evals import runner as runner_mod

        factory, _ = self._session_factory()
        run = self._run_row()
        execute = AsyncMock()
        finish = AsyncMock()
        with (
            patch(
                "turncall.storage.repositories.eval_repo.get_run",
                AsyncMock(return_value=run),
            ),
            patch("turncall.storage.repositories.eval_repo.finish_run", finish),
            patch.object(
                runner_mod,
                "resolve_target",
                AsyncMock(side_effect=TargetError("agent not found")),
            ),
        ):
            await runner_mod.execute_run(
                run.id,
                session_factory=factory,
                settings=SimpleNamespace(),
                execute=execute,
            )
        execute.assert_not_awaited()
        assert finish.await_args.kwargs["status"] is EvalRunStatus.ERRORED
        assert finish.await_args.kwargs["error"] == "agent not found"

    async def test_a_simulation_runs_through_its_own_mapper(self) -> None:
        """#73. The definition decides which mapper reads the result: handing a
        simulation result to the scripted mapper would report nonsense
        confidently, which is why the kind is read from what parses rather than
        from the column copied at queue time."""
        run = self._run_row()
        run.kind = "simulation"
        run.resolved_scenario = {
            "definition": {
                "persona": "a caller who lost their booking",
                "goal": "recover it",
                "success": "the agent finds it",
            }
        }
        execute = AsyncMock(return_value=_simulation_result())
        finish, _ = await self._execute(run, execute)
        kwargs = finish.await_args.kwargs
        assert execute.await_args.kwargs["kind"] is EvalKind.SIMULATION
        assert kwargs["status"] is EvalRunStatus.PASSED
        assert kwargs["results"][0]["goal"]["succeeded"] is True

    async def test_an_unparseable_stored_definition_errors_before_paying(
        self,
    ) -> None:
        """A definition that passed validation once can be made invalid by an
        update; the worker must not discover that per iteration."""
        run = self._run_row(iterations=5)
        run.resolved_scenario = {"definition": {"turns": "nope"}}
        execute = AsyncMock()
        finish = AsyncMock()
        await self._execute(run, execute, finish=finish)
        execute.assert_not_awaited()
        assert finish.await_args.kwargs["status"] is EvalRunStatus.ERRORED

    async def test_the_judge_that_ran_is_recorded_on_the_run(self) -> None:
        """A verdict is not comparable across time without it."""
        execute = AsyncMock(return_value=_script_result(turns=[_turn_result()]))
        _finish, start = await self._execute(self._run_row(), execute)
        harness = start.await_args.kwargs["harness_config"]
        assert harness["judge_model"], "judge_model must never be null"
        assert harness["judge_service"]

    async def test_a_run_that_is_no_longer_queued_is_left_alone(self) -> None:
        """Cancelled while queued, or already claimed by another worker."""
        execute = AsyncMock()
        finish = AsyncMock()
        await self._execute(self._run_row(status="cancelled"), execute, finish=finish)
        execute.assert_not_awaited()
        finish.assert_not_awaited()
