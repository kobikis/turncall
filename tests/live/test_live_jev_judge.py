"""The `jev` judge, against TypeSafe's real classification model (#141).

The only thing that can catch a contract change at the vendor: pipecat builds
the questions, Jev answers them, and a shape that moved surfaces as a
`ClassifierError` rather than as anything hermetic. Built the way a run builds
it — the compiled `judge.eval:` block, through `EvalJudge.from_config` — so the
credential path, the explainer decision and the factory are all the ones a
worker would use.

Deliberately **not** asserted: that Jev agrees with an LLM judge. Two judges
disagreeing is data about the scenario, not a defect. What is asserted is that
a verdict comes back at all, that it is one of pipecat's three, and that its
confidence is a real probability — which is the whole reason for adopting a
calibrated classifier.

Cheap: two classifications. Live: needs TYPESAFE_API_KEY and the `evals-jev`
extra. Skipped without either.
"""

from typing import Any

import pytest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

CONVERSATION = [
    ("user", "hi, what are your opening hours?"),
    ("bot", "We're open Monday to Friday, nine to five."),
]


def _judge(judge_block: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The judge a run would build from a scenario naming `provider: jev`.

    `with_models` compiles the typed block and decides the explainer; pipecat's
    parser lifts `judge.eval` onto the scenario; `EvalJudge.from_config` reads
    it. Every step the worker takes, and no transport.
    """
    from pipecat.evals.judge import EvalJudge

    from turncall.evals.scenario import parse, with_models

    definition = with_models(
        {"turns": [{"user": "hi", "expect": [{"event": "llm_response"}]}]},
        judge=judge_block,
    )
    parsed = parse(definition, name="live jev")
    return EvalJudge.from_config(parsed.judge), parsed.judge


async def test_a_verdict_comes_back_with_a_real_probability(typesafe_key) -> None:
    judge, block = _judge({"provider": "jev"})
    # No platform judge here, so reasons are off rather than resolving to a
    # local Ollama nobody configured (#139).
    assert block["factory"] == "turncall.evals.judges.jev"
    assert block["explainer"] is False

    for role, text in CONVERSATION:
        if role == "user":
            judge.add_user_message(text)
        else:
            judge.add_assistant_message(text)
    try:
        verdict = await judge.evaluate("the agent states its opening hours")
    finally:
        await judge.close()

    assert verdict.verdict in {"yes", "no", "continue"}
    assert verdict.confidence is not None, "a calibrated probability is the point"
    assert 0.0 <= verdict.confidence <= 1.0


async def test_a_reply_that_misses_the_criterion_is_judged_on_its_own_terms(
    typesafe_key,
) -> None:
    """One scenario, one criterion, a reply that does not meet it. The
    assertion is about the *pair* — a verdict and a probability — not about
    agreeing with any other judge."""
    judge, _block = _judge({"provider": "jev"})
    judge.add_user_message("can I book a table for six on Friday?")
    judge.add_assistant_message("Our address is 14 Mill Lane.")
    try:
        verdict = await judge.evaluate("the agent books the table or offers a time")
    finally:
        await judge.close()

    assert verdict.verdict in {"yes", "no", "continue"}
    assert verdict.confidence is not None
    assert 0.0 <= verdict.confidence <= 1.0
