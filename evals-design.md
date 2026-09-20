# Evals — design

**Status:** design, not built. Some decisions are settled (marked ✅), some are
open (marked ❓ with a recommendation). Nothing here has an ADR yet; the ADRs
come when the open questions close.

Automated behavioural testing for agents: scripted decision checks and
improvised end-to-end conversations, run against the real pipeline over text or
audio, judged by an LLM.

---

## 1. Why

Voice agents are probabilistic, and the pipeline construction path is where the
regressions actually live. The last four merged fixes were all in it:

| Fix | What broke | Would an eval catch it? |
|---|---|---|
| #64 | STT sent Deepgram's model name to every provider | Yes — 400 at STT connect. **Audio mode only** |
| #65 | LLM sent OpenAI's model name to every provider | Yes — Anthropic `404 not_found_error`. Text mode is enough |
| #63 | A default google S2S agent sent OpenAI's model to Gemini | Yes — provider rejects at connect |
| #67 | Two silence timers ran in series (+1.8s/turn) | **Likely**, via a `within_ms` budget in audio mode — unverified, see §9 |

Three confirmed, one likely. All of them sit in `pipeline_factory.py`, which is
exactly what this design exercises.

## 2. Vocabulary

Terms to add to `CONTEXT.md` as they settle. Listed here so they are used
consistently in the meantime.

- **Scenario** — one saved test. Exactly one of two **kinds**.
- **Scripted** (kind) — a fixed conversation with per-turn expectations. Answers
  *"at this point, did the agent make the right next decision?"*
- **Simulation** (kind) — a persona, a goal and success criteria; an LLM plays
  the caller and improvises. Answers *"by the end, did it reach the right
  outcome?"*
- **Persona** — the simulated caller's character and behaviour. Not the agent.
- **Judge** — the LLM that decides a verdict. Distinct from the persona; both are
  LLMs TurnCall runs, neither is the agent under test.
- **Modality** — `text` (no STT, no TTS) or `audio` (real speech both ways).
- **Iteration** — one execution of a scenario. Simulations need several; one
  proves nothing.
- **Run** — one scenario × target × modality, over N iterations. **Batch** — the
  runs produced by one request.
- **errored** vs **failed** — `failed` is the agent falling short. `errored` is
  the harness not completing (connect failure, judge outage). An errored run is
  neither a pass nor a fail and never counts toward a rate.
- _Avoid_: "test suite" — the name of the dead stub this replaces (§10) and of
  the abstraction Vapi retired in favour of Simulations.

## 3. Engine: `pipecat.evals`

Pipecat 1.11 is already a dependency and ships the framework (`pipecat/evals/`).
Reused rather than rebuilt.

What it provides: two scenario kinds sharing one loader; per-turn expectations
with `within_ms` latency budgets; `function_call` assertions (name + argument
subset); an `eval:` LLM-judge assertion; `send_after` + `bot_interrupted` for
barge-in; DTMF; simulation metrics (judged criteria with `min_score`, or
measured); a shared `EvalAssertionFailure` vocabulary across both kinds;
`CachingTTSService` for synthesized caller audio.

Independent of the file format: `EvalScriptScenario` and
`EvalSimulationScenario` are dataclasses, and

```
_parse_script(data: dict, path: Path)      -> EvalScriptScenario
_parse_simulation(data: dict, path: Path)  -> EvalSimulationScenario
_load_mapping(path) -> dict                # the only YAML in the stack
```

`load()` is just `_parse_*(_load_mapping(path), path)`. A JSONB column is
already the dict those parsers want, so **there is no YAML anywhere in this
design.**

## 4. Decisions

### ✅ Settled

- **One `scenario` entity, kind-discriminated.** Not two resources. The kind is
  computed eagerly at the API boundary (exactly one of `turns` / `persona`
  present) and stored as a column, so readers switch on an explicit value rather
  than sniffing nullability. Matches pipecat's own `EvalKind`, which it carries
  into the session, the driver and the result record.
- **Text *and* audio in v1.** Modality is a field on the run, not a second
  entity. Audio is not deferrable: it is the only thing that catches the #64
  class, and it is where a voice platform's risk actually sits.
- **The `test_suites` / `test_runs` stub is deleted**, not migrated (§10).
- **REST + CLI.** REST is the platform contract; the CLI is what makes evals get
  run. A publish-time gate is deferred until the flake rate is measured.
- **JSON, not YAML.** The CLI reads files byte-identical to the API request
  body. One schema, one validator.

### ✅ Settled (round 2)

- **Execution topology (Q6): a dedicated `turncall-eval-worker` process**, never
  the API process. ADR-0004 is the reason — eval pipelines must not share an
  event loop with live calls.
- **Judge (Q8): pipecat's `EvalJudge` only.** The drivers call it internally, so
  a second judge is duplication. Consequence accepted: the **judge** is
  OpenAI-family even when the agent under test is Anthropic or Bedrock, which
  makes **Ollama a first-class documented path**, not a footnote (§9.3).
- **Targets (Q9): all three** — `agent_id`, `agent_name@latest-published`,
  inline. Inline is load-bearing: it is the tool sandbox (§9.2), not a
  convenience.
- **Judge and persona cost (Q10): the platform `OPENAI_API_KEY`, with a
  per-project run cap.** Requiring a customer key makes the feature unusable on
  day one; the cap is what bounds the bill.
- **`eval_run_id` in the webhook envelope (Q11): yes**, additive and nullable,
  exactly as `session_id` was. ADR-0007's rule is that identity lives in the
  envelope; the alternative contradicts it.
- **Glossary and ADR ship in v1 (Q12).** `CONTEXT.md` entries per §2, and an ADR
  for the transport bridge and the worker split.
- **Scenario library (Q13): both.** TurnCall stores scenarios for direct API
  customers (whose project *is* their tenant); `POST /v1/eval-runs` also accepts
  an inline scenario body, which is what the builder posts from its own
  workspace-scoped library. Nothing is ever copied across projects, and TurnCall
  stays the headless engine its charter requires.
- **Tool mocking is in v1 (Q14).** `pipecat.evals` has none, so without it a
  scenario fires real side effects every iteration (§9.2). A `tool_mocks` map
  short-circuited in `orchestrator/tool_bridge.py` — nothing needed from
  pipecat.

### ✅ Settled (round 3)

- **`tool_mocks` live on the scenario (Q15), not the run.** A mock is part of
  what the test *means*: "given the booking succeeds, does it report the
  confirmation correctly?" is a different test from "given it fails, does it
  avoid claiming success?". Run-level mocks would let one scenario mean
  different things run to run — destroying comparability — and would need a
  fourth snapshot column to stay interpretable. Scenario-level is captured by
  `resolved_scenario` for free.
- **Unmocked tool calls fail closed (Q16).** A `tool_policy` on the scenario:
  `mock_only` (default) ends the run as `errored` with
  `"unmocked tool: <name>"` and never executes; `live` is an explicit opt-in for
  agents whose tools are read-only lookups. One enum rather than a mandatory
  mock per tool, and the dangerous path requires someone to type the word.

Both are **TurnCall columns, not keys inside `definition`.** `definition` is
versioned against pipecat's schema (`schema_version`); mixing ownership would
mean a pipecat 2.0 migration has to preserve foreign keys inside a mapping it
does not own. (Pipecat's parser is lenient about unknown keys, so this is a
hygiene decision, not a crash avoidance one.)

### Frontier

Empty. Everything above is settled; everything else is explicitly out of v1
(§8, §11).

## 5. Architecture

### The bridge

Pipecat's harness is an **RTVI WebSocket client**; the bot hosts `EvalTransport`
(a `SingleClientWebsocketServerTransport`) and the harness connects with
per-connection query flags — `skip_tts` (text mode: silences the bot, greeting
included), `capture_bot_audio` (audio mode), `trigger_disconnect`.

That is the whole coupling, and it is cheap here because the pipeline takes its
transport injected:

```python
create_pipeline(config, transport: Any, call_context, ...)
build_call_pipeline(*, config, transport, call_context, settings, session_factory, ...)
```

So the bridge is a fourth function beside the three in `transport_factory.py`:

```python
def create_eval_transport(port: int = 0) -> Any:
    """Loopback WS server transport for eval runs. port=0 -> ephemeral."""
```

and a run is both halves in one process:

```python
transport = create_eval_transport(port=0)
session   = await build_call_pipeline(config=resolved, transport=transport, ...)
bot       = asyncio.create_task(session.start())

harness = EvalScriptSession.from_scenario(scenario, f"ws://127.0.0.1:{port}")
result  = await harness.run()      # EvalScriptResult | EvalSimulationResult
```

Going around the socket would mean reimplementing the matcher, judge, persona
driver, latency budgets and event stream. The socket is the cheap price for all
of it.

**The property that makes this worth building:** only the transport is swapped,
so the eval runs the real `_create_stt_service` (with its per-provider keyterm
mapping), the real `_create_llm_service` (with the Anthropic-no-temperature
rule), the real VAD and smart-turn wiring, the real tool bridge, KB retrieval
and handoff.

### Where it runs

A **separate process**, never the API process. ADR-0004: Twilio paces audio in
hard realtime, so event-loop jitter becomes dead air on a live call. An eval run
does full STT+LLM+TTS at maximum speed and you want several at once. Same
container image, new entrypoint `turncall-eval-worker`, its own concurrency cap.

Queue: a Redis list — `LPUSH` on `POST /v1/eval-runs`, `BRPOP` in the worker. A
crashed worker orphans a `running` row, so a janitor sweeps rows past
`EVAL_MAX_SCENARIO_DURATION_S` into `errored`. No Celery.

**v1 builds a fresh pipeline per scenario-iteration.** `EvalSessionParams.stop_bot`
defaults to False and the transport is designed to serve several scenarios in a
row, which would be faster — but a scripted scenario resets context via its
`context:` field (an `LLMMessagesUpdateFrame` that *replaces* the context) while
a simulation has no equivalent, so cross-scenario leakage is a live hazard.
Isolation first; optimise when someone measures the cost.

### Known limit: S2S

An S2S pipeline has no TTS stage, so `skip_tts` has nothing to silence and there
is no separate LLM text output for a text-mode judge to read. **S2S agents are
expected to be audio-mode only** — the same shape as "avatar is WebRTC + cascade
only". Verify against a real Gemini Live agent before documenting it either way.

## 6. Schema

```sql
CREATE TYPE eval_kind       AS ENUM ('scripted','simulation');
CREATE TYPE eval_modality   AS ENUM ('text','audio');
CREATE TYPE eval_run_status AS ENUM
  ('queued','running','passed','failed','errored','cancelled');
CREATE TYPE eval_tool_policy AS ENUM ('mock_only','live');

CREATE TABLE eval_scenarios (
  id              uuid PRIMARY KEY,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name            text NOT NULL,
  description     text,
  kind            eval_kind NOT NULL,
  definition      jsonb NOT NULL,      -- pipecat's scenario mapping, verbatim
  schema_version  text NOT NULL,       -- pipecat schema the definition targets
  tool_mocks      jsonb NOT NULL DEFAULT '{}',   -- {tool_name: response}
  tool_policy     eval_tool_policy NOT NULL DEFAULT 'mock_only',
  tags            text[] NOT NULL DEFAULT '{}',
  default_target  jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX ix_eval_scenarios_project ON eval_scenarios (project_id);
CREATE INDEX ix_eval_scenarios_tags    ON eval_scenarios USING gin (tags);

CREATE TABLE eval_runs (
  id                  uuid PRIMARY KEY,
  project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  batch_id            uuid,
  scenario_id         uuid REFERENCES eval_scenarios(id) ON DELETE SET NULL,
  scenario_name       text NOT NULL,
  kind                eval_kind NOT NULL,
  target              jsonb NOT NULL,   -- what was asked for
  resolved_config     jsonb NOT NULL,   -- the agent config that actually ran
  resolved_scenario   jsonb NOT NULL,   -- definition + schema_version + tool_mocks + tool_policy
  harness_config      jsonb NOT NULL,   -- judge + persona model, pipecat version
  agent_id            uuid,             -- NULL for an inline target
  modality            eval_modality NOT NULL,
  iterations          int NOT NULL DEFAULT 1,
  status              eval_run_status NOT NULL DEFAULT 'queued',
  passed_count        int NOT NULL DEFAULT 0,
  failed_count        int NOT NULL DEFAULT 0,
  results             jsonb NOT NULL DEFAULT '[]',
  error               text,
  queued_at           timestamptz NOT NULL DEFAULT now(),
  started_at          timestamptz,
  completed_at        timestamptz
);
CREATE INDEX ix_eval_runs_project_queued ON eval_runs (project_id, queued_at DESC);
CREATE INDEX ix_eval_runs_batch          ON eval_runs (batch_id);
CREATE INDEX ix_eval_runs_scenario       ON eval_runs (scenario_id);
```

Six decisions worth defending:

**`definition` is opaque JSONB.** It *is* the mapping `_parse_script` /
`_parse_simulation` consume. Columns would mean tracking pipecat's schema in
Alembic forever, and that schema moves (1.11 deprecated top-level
`turns:`/`persona:`; 2.0 removes it). Validate at the API boundary by
round-tripping through the parser — fail fast with pipecat's own error — and
store what you were given. Cost: no cross-scenario queries.

**`schema_version`** exists because scenarios are durable data and pipecat's
schema is not stable. Without it, a 2.0 upgrade silently breaks stored
scenarios.

**Three snapshots on every run** — `resolved_config`, `resolved_scenario`
(definition, `schema_version`, `tool_mocks`, `tool_policy`), and
`harness_config`. A result is uninterpretable without knowing exactly what ran
on all three sides: the agent may be edited or archived, the scenario and its
mocks may be edited, and the judge model determines the verdict. This is ADR-0017's second
rule ("the config is stored raw… post-call code must go through it") applied one
level out.

**`agent_id` is NULL for inline targets** — ADR-0017's first rule, unchanged. No
locally invented sentinel in the column.

**No `score` column.** A scripted scenario yields pass/fail; a simulation yields
a rate over N iterations. `passed_count` / `failed_count` out of `iterations`
represents both honestly — `1/1` or `7/10` — with no column meaning two
incomparable things.

**`errored` is distinct from `failed`.** Pipecat draws this line itself
(`EvalSimulationResult.error` is "neither a goal success nor a goal failure";
`EvalScriptResult.skipped` is "neither passed nor failed"). A judge outage
reading as an agent regression is how a suite loses its audience.

**No `eval_suites` table.** Grouping is the `tags` column plus `batch_id`; Vapi
has no suite for Evals either ("separate saved Evals… group and run them
yourself"). A column, not a table.

### `results` entry — one per iteration

```jsonc
{
  "iteration": 1,
  "passed": true,
  "duration_ms": 4120,
  "transcript": [{"role": "user", "content": "..."}],
  "failures": [{"turn_index": 2, "expectation_index": 0,
                "event_name": "function_call", "kind": "no_match",
                "reason": "expected transfer_call, got none"}],

  // kind = scripted
  "turns": [{"index": 0, "passed": true, "failures": []}],
  "skipped": null,

  // kind = simulation
  "goal":    {"succeeded": true, "reason": "..."},
  "metrics": [{"name": "never_promised_refund", "passed": true,
               "score": 1.0, "reason": "..."}],
  "ended_by": "persona_end_call",
  "end_call": {"success": true, "reason": "booking moved"}
}
```

`EvalAssertionFailure` is the one result type pipecat shares across both kinds —
that shared failure vocabulary is why one envelope works while the middles
differ. `events_seen` and `debug_log` are large and only useful post-mortem:
store on failure only, or behind `?verbose=true`.

## 7. API

```
POST   /v1/eval-scenarios          # 201; validated by round-tripping the parser
GET    /v1/eval-scenarios          # ?kind= ?tag=
GET    /v1/eval-scenarios/{id}
PUT    /v1/eval-scenarios/{id}
DELETE /v1/eval-scenarios/{id}     # runs survive (scenario_id -> NULL)

POST   /v1/eval-runs               # 202 Accepted
GET    /v1/eval-runs               # ?batch_id= ?scenario_id= ?status=
GET    /v1/eval-runs/{id}
DELETE /v1/eval-runs/{id}          # cancel while queued/running
```

```jsonc
// POST /v1/eval-runs — scenario_id, tag, or an inline scenario body
{ "tag": "pre-publish",
  "target": {"type": "agent_name", "name": "support"},
  "modality": "audio",
  "iterations": 5 }

// tool_mocks and tool_policy belong to the scenario, not the run (Q15/Q16):
// POST /v1/eval-scenarios
{ "name": "confirms-booking-accurately",
  "kind": "scripted",
  "definition": { ...pipecat mapping... },
  "tool_policy": "mock_only",
  "tool_mocks": {"bookAppointment": {"status": "success", "id": "APT-1"}} }

// 202
{ "success": true,
  "data": {"batch_id": "…",
           "runs": [{"id": "…", "scenario_name": "…", "status": "queued"}]} }
```

Async-with-polling matches Vapi. The `{"success": true, "data": …}` envelope and
project scoping are unchanged.

### Targets

```jsonc
{"type": "agent",      "agent_id": "uuid"}      // an AgentRow *is* a version
{"type": "agent_name", "name": "support"}       // latest published, at run time
{"type": "inline",     "agent": { ...config }}  // transient; also the tool sandbox (§9.2)
```

### Modality

Pipecat splits `user.modality` and `judge.modality` independently — four
combinations. Expose one knob: `text` (both text) or `audio` (both audio). The
asymmetric pair `user: audio, judge: text` — exercises STT, skips TTS, the cheap
way to catch the #64 class — stays reachable through the scenario's own
`user:` / `judge:` blocks.

### Events

```
EVAL_RUN_STARTED   = "eval.run.started"
EVAL_RUN_COMPLETED = "eval.run.completed"   # comprehensive, carries the result
```

`eval.run.completed` follows `call.ended`'s precedent from ADR-0006: one
comprehensive terminal event, not a scatter of partials. Needs `eval_run_id` in
the envelope (Q11). `agent_id` resolves as always and is NULL for inline —
ADR-0017 reused, not a new special case.

## 8. Surfaces

### CLI

```bash
turncall eval run --tag pre-publish --target agent-name:support --modality audio --iterations 5
turncall eval run --scenario asks-for-account --target agent:0c3f… --modality text
turncall eval run ./evals/*.json          # inline bodies, nothing stored
turncall eval list --batch <id>
turncall eval show <run-id>
```

Streams progress; **exits 0 iff every run passed**. That single property is what
makes it usable from CI and from a coding agent.

### Console (`turncall-builder-web`)

The agent page already has `Config · Test · Calls · Knowledge · Takeaways ·
Code`, and `TestTab.tsx` is *manual* testing (chat over the Chat API, or a live
WebRTC call). Evals are the systematic half of the same job and sit next to it.

1. **`agents/:id/evals`** — scoreboard for this agent: last-run summary, modality
   toggle, iterations stepper, Run all, and a scenario table (name · kind badge ·
   tags · last result · duration · per-row run). Results render per kind: a tick
   for scripted, `7/10` for a simulation — never a fabricated percentage.
   `RequireRole min="editor"`, matching the existing rule that viewers cannot
   place test calls. Viewers still read results.

2. **Run detail — a transcript with assertions inline, not a JSON blob.** The
   most important screen and the easiest to get wrong. Conversation on the left,
   each agent turn annotated with its expectations (tick/cross, reason,
   expected-vs-actual). For simulations: goal verdict with the judge's reason at
   the top, metrics with scores, and the persona's own `end_call` claim marked
   **advisory** — pipecat is explicit that the judge decides. For audio runs:
   a player per turn **and the transcription the judge actually read**, shown
   beside the text, because that difference *is* why an audio run fails where a
   text run passed. N iterations render as a clickable dot strip.

3. **The on-ramp, and the highest-leverage part of the whole feature.** A library
   only exists if it is cheap to populate:
   - **"Save as scenario" on the Test tab** — `TestTab.tsx` already holds the
     messages in state; convert the session into a scripted scenario with
     suggested `eval:` criteria the user edits.
   - **"Save as scenario" on a `CallsTab` row** — a real production call becomes
     a regression test. The transcript and `tool_invocations` are already stored
     and are exactly the shape of a scripted scenario. This is Vapi's headline
     pitch, and TurnCall is structurally closer to it.

4. **Workspace-level "Scenarios" in the sidebar** (per Q13): the shared library,
   with "Run against…" and an agent multi-select — the screen you open before a
   model upgrade.

5. **Authoring is a form, JSON is the escape hatch.** Scripted: alternating
   User/Agent rows, each agent row with a collapsed *expect* block offering
   contains / matches regex / calls tool (name + argument subset) / judge says,
   plus an optional `within_ms` budget. Simulation: persona, goal, success, plus
   optional metric rows. A JSON toggle round-trips the raw `definition`.

6. **Last-run status beside Publish** on the Config tab — `10/12 passing · run
   now`. Information at the decision point, not a gate.

7. **Three statuses, three treatments.** passed green, failed red, **errored grey
   with "couldn't run"** — never a red X, never in a rate.

8. **Cost before the click.** "10 conversations · audio · ~6 min". Not a billing
   meter; just no surprises.

Not in v1: trend charts, flakiness detection, run diffing, scheduled runs.

## 9. Limitations

These belong in the user-facing docs on day one. A green suite that is quietly
narrower than people assume is worse than no suite.

### 9.1 Evals test the agent, not the call

Only the transport is swapped — so everything *in* the transport is invisible:

- **The Twilio serializer** (μ-law ↔ PCM16, 8kHz). The whole ADR-0004 bug class —
  scrambled audio, resampler-state threading — is **untestable this way**.
- **Output underrun.** Twilio paces in hard realtime; a loopback socket does not.
  Evals measure *latency*; they cannot detect *dead air*.
- **Telephony:** DTMF over Twilio, AMD, warm-transfer whisper, `<Dial>`, status
  callbacks. The `transfer_call` tool call is assertable; the bridge is not.
- **Barge-in, partially.** `send_after` + `bot_interrupted` works, but with
  synthetic timing over loopback rather than real VAD on a noisy 8kHz line.

### 9.2 Tools fire for real — and there is no mocking in pipecat

Verified: `pipecat.evals` has **no tool mocking**. `function_call` is an
assertion; `context:` seeds prior LLM messages. Neither intercepts a live call.
A scenario exercising "book the appointment" against an agent with a real
`webhook_url` **books a real appointment**, every iteration. Same for MCP
servers, discovered and connected per call exactly as in production.

Three answers, weakest first:

- **`context:` pre-seeding** — arrive at a checkpoint with fabricated tool
  results already in context, so nothing fires. Covers most decision checks, but
  not an assertion whose subject *is* the call.
- **Inline targets with sandbox tools.** This is why inline is load-bearing
  rather than a convenience.
- **Tool mocking in TurnCall — in v1 (Q14/Q15/Q16).** TurnCall owns
  `orchestrator/tool_bridge.py` and `services/chat_tools.py`. The scenario's
  `tool_mocks` map reaches the bridge via `CallContext` and short-circuits the
  call; under the default `tool_policy: mock_only` an unmocked tool ends the run
  as `errored` rather than executing. Needs nothing from pipecat, and is the
  difference between a feature people run and one they fear.

### 9.3 The judge is the weakest component

- **Non-determinism.** Verdicts are cached within a run, not across runs, so a
  scenario can flip with no code change. Binary criteria and iterations reduce
  it; nothing removes it.
- **Model drift.** A silent provider-side model update moves the whole baseline.
  Pin a dated snapshot; record it in `harness_config`.
- **OpenAI-family only.** `EvalJudge` needs `run_inference()`. A customer on
  **Bedrock for data residency** cannot run evals without sending transcripts to
  OpenAI. **Ollama is the answer and must be documented as first-class**, not a
  footnote.
- **Side effects are unverifiable.** A tool reporting success does not prove the
  external system changed. With mocking, you are testing the agent's *narration*.
- **Simulations have three nondeterministic actors** — persona, agent, judge. A
  lazy or off-script tester reads as a bad agent, and `errored` does not catch
  that.

### 9.4 Text mode inverts the coverage

Text mode cannot see STT, TTS, VAD/turn timing, interruption, pronunciation, or
how numbers and dates are spoken — most of what makes this a voice platform. And
text is the mode people will actually run per-PR, because it is fast and free.
The cheap layer that runs constantly covers the smallest share of the risk. Plan
for audio runs on a schedule, not only on demand.

### 9.5 Throughput

The worker runs the bot pipeline **and** the harness — which itself runs a
persona LLM, a TTS, an STT and the judge. In audio mode that is roughly double a
real call's service load in one event loop, so a concurrency cap of 4 is
optimistic until measured. Fresh-pipeline-per-iteration (§5) means 12 scenarios ×
10 iterations = 120 pipeline builds. A hung pipeline holds its slot until the
janitor sweeps it.

### 9.6 Non-technical

Suites rot, and nothing flags a scenario that has not run in 60 days or has
passed 400 times. Someone still has to write the assertions — "Save as scenario"
captures the conversation, not the judgement. And evals only find what you
thought to test; unknown unknowns come from production, which is why the
`CallsTab` → scenario button is the loop that matters.

## 10. Removing the stub

`test_suites` and `test_runs` exist today with four endpoints in
`api/v1/testing.py`. Nothing executes them — the docstring promises "a background
worker" that was never written, and there are no references outside the router
and the models. They shipped in the initial schema and were never touched.

One migration and one commit: drop both tables and `test_run_status`; delete
`api/v1/testing.py` and `api/v1/schemas/testing.py`; remove `TestSuiteRow` /
`TestRunRow` from `storage/models.py`; remove the two `include_router` lines in
`app.py`. The downgrade either recreates them or the drop is declared one-way.

## 11. Phasing

1. **Delete the stub.** Independent, unblocks the namespace.
2. **`create_eval_transport` + the worker**, scripted kind, text modality, one
   iteration. Proves the bridge end to end.
3. **Tool mocking** in `tool_bridge`. Before anyone points a scenario at a real
   agent.
4. **Audio modality.** Then verify the #67 latency claim and the S2S limit for
   real.
5. **Simulation kind** — persona, goal, metrics, iterations.
6. **CLI** with the exit code.
7. **Console**: evals tab, run detail, then the two "Save as scenario" buttons.
8. **ADRs and `CONTEXT.md`** as each decision closes.

## 12. References

- `pipecat/evals/` in the venv (1.11.0); docs at docs.pipecat.ai/pipecat/evals
- Vapi: `docs.vapi.ai/test/voice-testing`, `/observability/evals-quickstart`,
  `/observability/simulations-overview`
- `adr/0004` (audio QoS — why the worker is a separate process), `adr/0006`
  (one comprehensive terminal event), `adr/0007` (envelope vs payload),
  `adr/0013` (takeaways — the deferred scorer), `adr/0017` (calls without an
  agent row — the snapshot rule)
- builder `docs/adr/0011` (Workspace is the tenant; a TurnCall project is
  per-agent plumbing — the reason for Q13)
