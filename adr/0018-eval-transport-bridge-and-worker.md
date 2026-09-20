# 0018 — The eval transport bridge, and the worker that is not the API

Agent evals (#68) run a saved scenario against an agent and score it. The two
structural decisions that shape everything built on top are: *where the
scenario meets the pipeline*, and *which process runs it*. Both are settled
here; the surface above them (scenario kinds, modalities, mocks, targets) is
ordinary feature work that assumes these.

## The bridge

`pipecat.evals` ships the whole harness we would otherwise write: two scenario
kinds sharing one loader, per-turn expectations with `within_ms` latency
budgets, `function_call` assertions, an `eval:` LLM-judge assertion, a persona
driver, `send_after`/`bot_interrupted` for barge-in, DTMF, simulation metrics,
and one shared `EvalAssertionFailure` vocabulary across both kinds.

Its harness is an **RTVI WebSocket client**. The bot hosts `EvalTransport`, a
single-client WebSocket server, and the harness connects with per-connection
query flags — `skip_tts` (text mode: silences the bot, greeting included),
`capture_bot_audio`, `trigger_disconnect`.

That is the entire coupling, and it is cheap here because our pipeline already
takes its transport injected:

```python
create_pipeline(config, transport, call_context, ...)
build_call_pipeline(*, config, transport, call_context, settings, ...)
```

So the bridge is a fourth function beside the three in `transport_factory.py`
(`create_eval_transport`), and a run is both halves in one process: build the
agent's real pipeline against an eval transport, start it, point pipecat's
session at `ws://127.0.0.1:<port>`, await the result.

**Why not go around the socket.** The alternative is feeding the pipeline
frames directly and reading its output. That means reimplementing the matcher,
the judge, the persona driver, the latency budgets and the event stream — every
one of which pipecat already has, tested, and will keep current. A loopback
socket is a cheap price for all of it.

**The property that makes this worth building at all: only the transport is
swapped.** An eval therefore runs the real `_create_stt_service` (with its
per-provider keyterm mapping), the real `_create_llm_service` (with the
Anthropic-no-temperature rule), the real VAD and smart-turn wiring, the real
tool bridge, KB retrieval and handoff. That construction path is where #63,
#64, #65 and #67 all lived. An eval that stubbed it would test nothing that has
ever actually broken.

### An eval is not a call

The pipeline is real but there is **no `calls` row**, and there must not be one:
an eval must not appear in the customer's call list, must not bill, and must
not dispatch `call.started` / `transcript.final` / `call.ended` to their webhook
subscribers for a conversation that never happened.

`CallContext` therefore carries `eval_run_id`, and `is_eval` gates every
call-scoped side effect — `CallSession._update_call_status`,
`CallSession._finalize_call` (which is also what dispatches `call.ended` and
triggers post-call analysis), and the two transcript taps in
`observability.py`. The eval's record is the `eval_runs` row; the conversation
is observed by the harness over the wire.

This is the same shape as ADR-0017: a pipeline whose usual anchor row does not
exist is a normal case that reads like an impossible one. The guard is one
property, checked in four places, rather than four independent judgements.

### A fresh pipeline per scenario-iteration

`EvalSessionParams.stop_bot` defaults to False and the transport is built to
serve several scenarios in a row, which would be faster. We do not: a scripted
scenario resets context via its `context:` field (an `LLMMessagesUpdateFrame`
that *replaces* the context) while a simulation has no equivalent, so
cross-scenario leakage is a live hazard and a leaked turn would show up as a
mysterious flake rather than an error. Isolation first; optimise when someone
measures the cost.

The cost is real and should be named: 12 scenarios × 10 iterations is 120
pipeline builds, each opening its own provider connections.

## The worker

**Eval pipelines never run in the API process.** ADR-0004 is the reason: Twilio
paces audio in hard realtime, so event-loop jitter on the API process becomes
dead air for a person on a live call. An eval run does full STT+LLM+TTS at
maximum speed and the whole point is to run several at once — precisely the
load that produces that jitter.

Same container image, new entrypoint `turncall-eval-worker`, its own
concurrency cap (`EVAL_MAX_CONCURRENT_RUNS`, default 4 — a starting point, not
a measurement: the worker runs the bot pipeline *and* the harness, which itself
runs a persona LLM, a TTS, an STT and the judge).

**Queue: a Redis list.** `LPUSH` on `POST /v1/eval-runs`, `BRPOP` in the worker.
No Celery — the job is a uuid. The run row is committed *before* the queue push,
so a Redis outage leaves a visible `queued` row rather than work that executed
but was never recorded.

**A janitor, because a crashed worker cannot clean up after itself.** It leaves
its run claimed at `running` forever, and nothing else would ever move it; to an
operator that reads as work still in flight. The janitor sweeps rows whose
`started_at` is older than `EVAL_MAX_RUN_DURATION_SECONDS` into **`errored`,
not `failed`** — nobody learned anything about the agent.

**Claiming is a conditional update.** `start_run` updates
`WHERE id = ? AND status = 'queued'`, so two workers racing the same id leave
exactly one winner and the loser finds the run no longer queued.

## Consequences

- A scenario's `definition` is pipecat's mapping, stored verbatim as JSONB and
  validated by round-tripping it through pipecat's own parser. We track
  `schema_version` beside it rather than modelling a schema that is not ours
  and that moves between majors. The cost is no cross-scenario SQL queries.
- We inherit pipecat's judge, so the **judge** is OpenAI-family even when the
  agent under test is Anthropic or Bedrock. A customer on Bedrock for data
  residency needs a self-hosted OpenAI-compatible endpoint; that is a
  first-class documented path, not a footnote.
- Pipecat's schema moving is now our migration problem, bounded by
  `schema_version`.
- **Text modality cannot see the agent's `first_message`.** It goes out as a
  `TTSSpeakFrame`, so it never becomes LLM text, and `skip_tts` silences the
  TTS — the harness sees no events at all. Verified, and pinned by a live test.
  Whether audio mode can see it is open (#72).
- Everything inside the transport stays untestable this way: the Twilio
  serializer and the whole ADR-0004 audio-corruption class, output underrun and
  dead air (a loopback socket does not pace in realtime, so evals measure
  *latency* and cannot detect *silence*), and the telephony layer. A green
  suite is narrower than it looks, and that belongs in the user-facing docs on
  day one.
