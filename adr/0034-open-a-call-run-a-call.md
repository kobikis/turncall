# ADR-0034: Open a call, run a call — one module below every voice edge

**Status:** Accepted
**Date:** 2026-11-15
**Relates to:** ADR-0017 (calls without an agent row), ADR-0021 (ambience),
ADR-0022 (the carrier boundary). PRD #487; #488–#496.

## Context

No module owned opening a voice call or running one. Each voice edge — the
carriers' media stream (Twilio, Telnyx, Asterisk, and outbound dials once
they connect), WebRTC, WhatsApp voice, the website demo — assembled its call
itself: take a live-call slot, write the call row with its `dynamic_config`,
record `call.initializing` and `call.started`, give the slot back if the row
is never written, count the call for the drain, hold the slot while it runs,
connect the agent's MCP servers and close them if the build fails, build the
ambience mixer, build the call context with the inline-agent sentinel, build
the pipeline and start it.

The copies drifted, and two gaps were live bugs. The media stream connected
MCP servers and did not close them when the build raised (#488), so a phone
call whose build failed left its MCP sessions open. WebRTC took its slot and
wrote the call row outside the code that freed it (#489), so a failed write
held a slot for its two-minute lease.

What held the copies together was a set of tests that read each entry
point's source and checked it remembered each step: the slot and its hold,
the mixer, the control channel, the inline-agent sentinel, the drain count.
A new edge had to learn every step and satisfy every one of those tests.

## Decision

**Two functions below the edges, with an opened call between them.**

- **Opening** — `services/call_opening.open_call(source, *, project_id,
  session)`. The `source` is what the edge resolved (a `CallSource`: the
  parties, the edge's `EdgeHandles`, and how the agent is chosen — a number's
  `NumberSource`, WebRTC's browser request, WhatsApp's connect). Opening runs
  agent acceptance (#474, not re-implemented), takes the live-call slot,
  writes the call row and its start events, and commits. It answers an
  `OpenedCall` (call id, project, accepted agent, the row's `CallMetadata`,
  the handles) or a `CallRefusal` (`live_call_limit`, `no_agent`,
  `agent_refused`), which each edge maps to its own reply. Any failure before
  the commit gives the slot back, so a slot is never held without a row.
- **Running** — `orchestrator/call_runner.run_call(call, make_transport, *,
  own_task)`. It takes a `CallToRun` (built from an `OpenedCall` by
  `CallToRun.of`, or by the media stream from the row) and the edge's
  `TransportFactory`. It counts the call for the drain, holds and renews the
  slot and frees it however the run ends, connects MCP and closes it on every
  path where the session will not (a failed build, a cancellation before the
  start), builds the mixer at the factory's rate, the `CallContext` and the
  pipeline, and starts it.
- **`own_task`.** An edge that must return from a connection callback while
  the call goes on (WebRTC, WhatsApp voice, the demo) runs the call in a task
  the module creates and keeps; `run_call` returns once the build succeeded,
  or raises what it raised, so WebRTC still answers `500
  pipeline_start_failed`. Without it the run blocks until the call ends (a
  carrier's media stream). Either way MCP is connected and closed in the
  same task, as its anyio cancel scopes require.

**What stays at the edges** is ADR-0022's: signature checks, protocol
parsing, the answer format and the transport factory. The media stream keeps
its front — the media token, the carrier check, the one-stream claim — and
accepts the row's agent again before it runs the call; that re-acceptance is
how a call opened in another request (an inbound webhook, an outbound dial)
becomes a call to run.

**Outbound dials keep their opener** (`place_outbound_call`: suppression,
limits, the carrier dial), and write their row and events through the same
`write_call_row` / `write_call_start`. The demo runs through `run_call` with
no database: no row, no slot, nothing recorded.

## Seams and tests

The module is tested through its interface, against the test Postgres and
Redis with stub providers and a fake transport factory, and each test checks
what the world sees afterwards — a slot held or not, which row and events
exist, whether the MCP fake was closed, whether the drain counted the call,
what reached the transport (`tests/integration/test_call_opening.py`,
`test_run_call.py`, `test_outbound_opening.py`,
`test_whatsapp_voice_opening.py`). The call harness and the carrier contract
test are unchanged and prove each edge hands its call to the module.

The source-reading wiring tests for the steps the module owns are deleted.
One structural check is left, the part the module's tests cannot see: an
edge that skips the module. `tests/unit/test_voice_edges_use_the_call_module.py`
fails, naming the module, on one that builds a voice transport without
`run_call`, builds a call's pipeline itself, or writes a call row outside the
opening module. `test_build_takes_accepted_agent.py` (#481) stays.

## Consequences

A lifecycle fix lands once. A new voice edge writes its parser, its answer
and its transport factory, and calls `open_call` and `run_call`.

Opening and running are separate on purpose: a carrier opens the call in its
inbound webhook and runs it in another request, when the media stream
connects, so the opened call is the row and the run reads it back. The cost
is the second acceptance at the media stream, which is cheap and is also the
only check an outbound dial's own agent gets.

`CallContext.records` still decides what a call records; the module adds no
switch of its own for the demo or an eval (an eval is not run through it at
all).
