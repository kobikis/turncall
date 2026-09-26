# ADR-0019: pipecat 1.12 — verdicts become classifications

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** nothing. Extends ADR-0018 (eval transport) and ADR-0014.

## Context

pipecat 1.12 introduces **Classifiers**: small objects that answer typed
questions about some state — yes/no, a choice among options, a score — and
return a probability with each answer. Two ship: `LLMClassifier`, over any
pipecat LLM service, and `JevClassifier`, over TypeSafe's purpose-built
classification model.

Two things TurnCall already runs were rewritten on top of them, and neither
rewrite is opt-in:

- **`EvalJudge` now decides every verdict with a classifier.** An LLM judge no
  longer answers a prose prompt; it gets the conversation as structured state
  and picks an outcome. A borderline reply can get a different verdict than it
  did on 1.11. A simulation is judged **one bot turn per call** instead of the
  whole run in one call.
- **`VoicemailDetector` is one `FrameProcessor` instead of a parallel pipeline
  with its own LLM branch.** `llm=` and `custom_system_prompt=` are deprecated
  behind a shim that goes away in 2.0.0.

The bump also forces `mcp>=2.1.1` (pipecat's `mcp` extra requires it),
`transformers>=5.10` (the `local-smart-turn` extra), and replaces `nltk` with
`sentencex` for TTS sentence aggregation.

## Decision

Take the bump whole, in one PR with no intended behaviour change, and adapt
rather than pin around any of it.

**1. The judge's verdicts change meaning, and that is accepted.** A verdict
that moves because pipecat changed how judging works is not a regression in the
agent, but it is indistinguishable from one on a single run. `harness_config`
already records the judge (#119) and already warns when it changes between runs
of the same scenario; the pipecat version is recorded alongside it. This is the
same class of event as a silent provider-side model update, which CONTEXT.md
already names as the judge's weakest property.

**2. A simulation now costs one judge call per bot turn, and the default judge
could not keep up.** A ten-turn simulation at ten iterations went from 10 judge
calls to ~100 — and those calls are fired *together*, one per bot turn.

That broke the default judge outright. `LLMClassifier` waits 10s; a local
Ollama serializes concurrent requests. Measured here with `gemma4:e2b`: **one
classification 4.5–5.8s, four concurrent 18s each**. So every turn of a
four-turn simulation timed out and the run reported `judge call failed` —
which, from the outside, is indistinguishable from the agent having regressed.

TurnCall's `ollama` judge factory therefore returns an `LLMClassifier` with a
**60s** budget instead of a bare LLM service. Pipecat accepts either
(`classifier_from_config`), and it leaves explainer resolution unchanged:
`EvalJudge.from_config` reads `classifier.llm` when an `LLMClassifier` arrives
without an `explainer:` block, which is the same Ollama a bare service would
have given. 60s is generous against the measured 18s and still a third of the
180s floor the per-iteration budget allows one conversation (ADR-0018), so a
genuinely hung judge is still caught by the budget.

This forced the provider allowlist to split by **role**. A judge only answers
questions; a persona is linked into a pipeline to speak, and handing it the
classifier wrapper raises `AttributeError: 'LLMClassifier' object has no
attribute 'link'` at pipeline build. So `PROVIDERS` (what a simulator may be)
and `JUDGE_PROVIDERS` (a superset) are two maps rather than one map plus a
check — which also makes "this provider cannot be a persona" a structural fact
rather than a validation rule, and is where `jev` will land.

The caveat that remains: a scenario using pipecat's **raw** `judge.eval:`
escape hatch (`service: ollama`) still gets the 10s budget, because TurnCall's
factory is not in that path. That is the documented trade of the escape hatch —
a raw block wins, deliberately — and it is now a reason to prefer the typed
block rather than a matter of taste.

`EVAL_MAX_CONCURRENT_RUNS` is unchanged: it bounds concurrent *runs*, which is
still the right axis.

**3. The voicemail detector is built with an explicit classifier.** Not left to
the deprecation shim, which is removed in 2.0.0 and would make this a surprise
then rather than a decision now. `LLMClassifier`'s `instructions` **replace**
its defaults rather than extending them, and the defaults are what ask for the
JSON object it parses — so an agent's `custom_system_prompt` is prepended to
them, exactly as the shim does. Passing it straight through would leave every
verdict unparseable, which surfaces only as "classification failed" in a log
line and a call that never detects voicemail.

Three runtime consequences, all accepted:
- Detection is **~1s later** (`decision_timeout`, new). Deliberate on pipecat's
  part: "hi, this is Sam" is both what a person says and how a recorded
  greeting starts, and only the silence that follows tells them apart. The
  existing `backoff_plan` was buying the same certainty with retries.
- A classifier that does not answer in time is read as **conversation**.
  Correct direction: treating a person as an answering machine is the expensive
  error.
- The handler's `processor` argument is now the detector itself, so the
  `TTSSpeakFrame` the voicemail handler pushes originates one slot earlier in
  the graph. It still reaches TTS and the gate.

**4. Jev is adopted for the judge only, as an optional extra.** `provider: jev`
joins the closed set in `evals/judges.py`, mapping to a TurnCall-shipped
factory exactly as `ollama`/`openai`/`anthropic` do — pipecat's own escape
hatch is `factory`, a dotted path handed to `importlib.import_module`, which
from a request body is remote code execution (#118). It is **not** adopted for
voicemail: that is on the live call path, and a new vendor there is a decision
that deserves its own measurement.

**5. `mcp` narrows to 2.x and the dual-spelling reads are deleted.** They
cannot execute under a pin that pipecat forces anyway, and CLAUDE.md advertised
the 1.x support as a feature. Code that cannot run is a false claim about what
the project supports.

## Consequences

- A scenario's verdicts may differ across the bump. `harness_config` is what
  makes that attributable; nothing else would.
- `evals-jev` is a new optional extra. Without it, `provider: jev` raises at
  run time rather than at scenario create — a key and an extra are
  environmental, and a stored scenario outlives the deployment it was written
  against (Q9).
- The `custom_system_prompt` → `instructions` composition is a trap with a
  silent failure mode, so it is guarded by
  `tests/unit/test_voicemail_classifier.py` rather than left to review.
- `SCHEMA_VERSION` moves to `pipecat-1.12`. Existing rows keep `pipecat-1.11`:
  the column records which parser validated a definition, and 1.12 changed no
  field of either scenario dataclass, so those rows are still valid.

## Alternatives considered

**Pin `transformers<5` and keep the old Smart Turn stack.** Rejected after
measuring: `LocalSmartTurnAnalyzerV3` loads a bundled ONNX model, not a
transformers one. It constructs in 0.1s and discriminates correctly under
transformers 5.12.1 (silence → turn complete at p=0.99, voiced-then-silence →
incomplete at p=0.03) in ~45ms. There was nothing to pin around.

**Keep `VoicemailDetector(llm=...)` and defer.** Rejected: the shim is removed
in 2.0.0, and deferring converts a decision made with the changelog open into a
breakage discovered during the next major.

**Adopt `JevClassifier` for voicemail too.** Deferred, not rejected. It would
replace an LLM turn on the live call path with a few-hundred-millisecond
classification, which is exactly the kind of latency the platform cares about —
but it introduces a vendor into the call path, and that belongs behind a
measurement rather than behind a changelog entry.
