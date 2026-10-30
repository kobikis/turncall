# ADR-0032: Procedures — the system holds the route, the model sees a window

**Status:** Accepted
**Date:** 2026-10-30
**Relates to:** ADR-0012 (knowledge base retrieval), ADR-0017 (inline agents).
PRD #417; #441, #442.

## Context

A troubleshooting, collections or other regulated voice agent has a procedure
it must follow step by step: questions in order, checks the caller performs,
conditions that pick the next step, and rules for escalating. The only place
to put one was the system prompt, and a long procedure in a prompt fails in a
predictable way: the agent follows it for a few turns, then loses its place
when the caller interrupts, answers indirectly or goes back a step. It skips a
confirmation and the call still sounds fine. Nothing recorded which step the
call reached.

Two designs were obvious and both were rejected:

- **The whole graph in the prompt.** That is the drift problem above, with
  more tokens on every turn.
- **A webhook tool that hands out one step per call.** Every step costs a
  tool round trip and a second inference; the wait between steps roughly
  doubles and the conversation follows commands rather than the caller.

## Decision

**TurnCall holds the route; the model is shown a window of it.** A procedure
is a graph of steps in the agent's config (`procedures`): each step has an
id, a kind (`ask`, `instruct`, `check`, `escalate`, `end`), its text, an
optional tool and its moves (`{to, when}`, a plain-language condition). The
call holds each running procedure's place in memory, in the call's pipeline
(`orchestrator/procedures.CallProcedures`, one immutable `ProcedureState`
replaced whole by a move).

**The window is one per-turn message.** Before each inference a processor
between the user aggregator and the LLM puts a `developer` message right
before the caller's turn, replacing the previous turn's — the same per-turn
replace KB auto mode uses (`turn_message.replace_turn_message`, #441), under
its own marker (`[Procedure]`). It holds the current step and what to do
there, the allowed moves with their conditions, the always-allowed move back
to the previous step, and a preview of the steps up to the procedure's
`window_depth` ahead. It is built by a pure function of the graph, the
visited path and the depth (`domain/procedure_window.build_window`), so it is
the same text on every carrier and on WebRTC: there is one processor in the
cascade pipeline and no transport branch.

**The model reports moves; TurnCall checks them.** A built-in tool,
`move_to_step` (`{procedure, to}`), is offered only to an agent with
procedures and never listed in `tools`. A move the current step allows, or
back to the previous step, is accepted: the state moves and the result
carries the new step's text and the step's own moves, since the inference
that follows a tool result gets no fresh window — a step whose tool answers
would otherwise leave the model not knowing it should move on (a live call
confirmed a booking, stayed on the step, and confirmed it again on the next
turn). A move to the current step is the model staying put (a caller asking
to hear it again): not a refusal, and no event. Anything else is refused:
the result names the allowed moves and why, and the state and window stay
put. The model judges the
condition; TurnCall checks only that the move is allowed. A move is checked
and applied with nothing awaited in between, so a barge-in cancelling the
tool cannot leave the state half moved.

**An `end` or `escalate` step finishes the procedure.** Reaching one takes the
procedure out of the running set: its window stops and every later move in
it, a move back included, is refused as finished. Without that a move back
from an `end` step would turn a completed procedure into one that is not, and
an escalation already transferred or handed off could be walked back on the
record while the call itself could not be. An escalate step's action is the
tool's own (`transfer_call` with its fields, the briefing included, or
`handoff_to_agent`), run through the path that tool takes; the window names
the escalate move as the way out when the procedure cannot resolve the
problem, since a model shown only the condition tends to keep trying.

**What a call records is derived from its events.** Each procedure started
is a `procedure.step` from nowhere to its start step — a `start: "call"` one
as the call starts, a handoff target's as the handoff applies, an
`on_request` one when the model starts it — then each move, refused move and
handoff that left it, in order. The call's `procedures` summary is folded
from those events alone, so a procedure the caller never got past the first
step of is still listed, at its start.

**A move beside speech costs no inference (#445).** The caller has already
been answered, so the move's result asks for none; a move with no speech
asks for one, so the caller always is. The decision is a processor right
after the LLM, not the tool handler: the handler runs before the response's
text has passed, and only a processor downstream of it sees the text and the
results in the order the assistant aggregator reads them. It sets `run_llm`
on every result of a response that reported a move — one inference after the
last call settles, or none for speech and only moves — because pipecat's
group rule ran the LLM twice when a move settled before a lookup beside it
had reached the aggregator. Being after the LLM group, it holds whichever
failover member answered.

**Procedures are config.** They version, publish, roll back and A/B-route
with the agent, and an inline agent (call-init, an eval target) carries its
own with no lookup. The API schema is strict; the domain model is
permissive, so a broken graph on an inline agent reaches the build, which can
say what is wrong (#444) instead of failing the parse before the call can.

## Consequences

- The prompt holds a few hundred tokens of window per running procedure
  instead of the whole graph, and the model never sees a step beyond the
  depth. A deeper `window_depth` trades prompt size for how much of the path
  the model can plan for.
- A move is a tool call, so the existing eval `function_call` expectation
  asserts it with no pipecat change.
- Every cascade pipeline carries the window processor, which does nothing
  for a call with no procedure running. A handoff can bring procedures to a
  call that started with none.
- Cascade voice only. A realtime model reads no per-turn message ahead of its
  turn, so S2S skips procedures (with a warning); text conversations are out
  of scope.
- Shipped in steps: the window and `move_to_step` (#442), then the graph rule
  (#444), events and the call summary (#443), no extra inference for a move
  beside speech (#445), `on_request` and several procedures (#446), template
  variables in steps (#447), escalation and handoff (#448), evals (#449).
