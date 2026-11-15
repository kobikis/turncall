# 0017 — Calls without an agent row

Managed call-init (ADR-0008 in the builder, and `/guides/call-init`) may answer
with an **inline agent** — a whole config in the response — instead of an
`agent_id`. The builder uses this to hand a call credentials that must not be
stored: an MCP `Authorization` header minted for that call and expiring within
the hour. There is nothing to store them *on*, because an inline agent is not a
row in `agents`.

So `calls.active_agent_id` is `NULL` for the life of such a call, and the config
it ran with exists nowhere durable unless the transport writes it down.

Both facts are easy to not know. Both were discovered the expensive way.

## What went wrong, twice

**A UUID that was not one.** `CallContext.agent_id` is typed `UUID` and is looked
up against the database. On the inline path `media_stream.py` built it from
`agent_id_str`, which on exactly that path was the literal string `"dynamic"` —
so every inline-agent call over Twilio died with `ValueError: badly formed
hexadecimal UUID string` before the pipeline started. WebRTC and WhatsApp voice
had each independently invented `UUID(int=0)` in the same place and worked.

**A webhook that never fired.** All three post-call trigger sites gated on
`call.active_agent_id` being set, loading the agent to get its config. For an
inline agent there was no agent to load, so post-call processing was skipped —
and since `trigger_post_call_analysis` is also what dispatches `call.ended`,
inline-agent calls emitted **no `call.ended` at all**. Not a degraded analysis: no
event, no transcript delivery, and nothing downstream of it.

Fixing the readers was not enough. WebRTC resolved its inline config, validated
an `AgentConfig` out of it, and then dropped it: with no `dynamic_config` on the
call row there was still nothing for the reader to find, so every browser call
with an inline agent — the builder Console's own preview button — finalized
silently. Two of three writers stored it; the third did not, and nothing said
they had to.

The shape of all three bugs is the same. A call without an agent row is a normal
case that reads like an impossible one, and each transport met it alone.

## Decision

**Two rules, and every voice transport honours both.**

1. **`DYNAMIC_AGENT_ID` is the sentinel**, defined once in
   `orchestrator/pipeline_factory.py` as `UUID(int=0)`. Every transport passes
   `agent_id or DYNAMIC_AGENT_ID` into `CallContext`. It is a real UUID because
   it is passed where one is declared, and it is the zero UUID because that
   matches no agent that exists — a lookup returns nothing rather than someone
   else's agent.

2. **The config the call ran with is persisted** on the call row, as
   `metadata_json["dynamic_config"]`, raw — before template rendering, the same
   as the other transports store it. `services.call_analysis_trigger.config_for_call`
   is the single reader: the agent's `config_blob` when there is an agent, that
   key when there is not.

`DYNAMIC_AGENT_ID` is deliberately *not* written to `active_agent_id`. The column
means "which stored agent is this call running", and a sentinel there would be a
foreign key to nothing and would make every consumer's null check wrong. Events
carry `agent_id: null` for such a call, which is the honest answer and what
ADR-0007 already specifies.

**The guard is structural.** `tests/unit/test_inline_agent_context.py` parses
each transport's source and asserts that every `CallContext(agent_id=…)` falls
back to the sentinel, that nobody reinvents `UUID(int=0)` locally, and that every
file creating a call row writes the `dynamic_config` key. Behavioural tests would
each have to know a transport's full setup; the rule is about a shape, and the
failure mode is a transport added later that quietly does neither.

Note that the guard keys off *the file that creates the call row*, which is not
always the file that builds the `CallContext` — Twilio answers the inbound
webhook in one request and opens the media stream in another.

## Consequences

Everything that runs after a call must go through `config_for_call`, never
`agent.config_blob` directly, and must tolerate `active_agent_id` being null. A
log line that dereferences the agent counts: one at `twilio_handlers.py:348` was
missed when the surrounding code was fixed, and an `AttributeError` in a log line
ends the call just as thoroughly as one anywhere else.

An inline agent costs the call its `active_agent_id`, and with it anything keyed
on the agent: per-agent call listings, handoff bookkeeping, and the `agent_id` on
every event for that call. That is the price of a config that never touches the
database, and it is why the builder inlines an agent only when the call actually
needs per-call credentials.

Knowledge-base attachments are loaded by `agent_id`
(`load_agent_kb_attachments`), so an inline agent gets none — the sentinel
matches no agent, and there is no row to have attached one to. Per-call context
still reaches the prompt through call-init's `dynamic_data.knowledge_context`,
which is the mechanism an inline agent should use for it.

## Addendum: the guard is structural at the build too (#481, 2026-11)

An inline config used to reach `build_call_pipeline` as a plain `AgentConfig`,
parsed by whichever transport held it, so every provider rule the API's schema
enforces (xAI's LLM and TTS, Grok Voice, Eleven Realtime, the Flux group,
procedures) had a second copy in the build, failing the call halfway into it.

The build now takes an **accepted agent** (`services/agent_acceptance`,
#474) and nothing else, and only that module constructs one. A config the
rules refuse never reaches the build: an inline agent is refused at call-init,
before the caller is answered; a stored agent saved before a newer rule is
refused where it is run (the carrier, a WebRTC 422, WhatsApp's reject, a
handoff's result, an eval run's error), and the media stream accepts the
call's agent again before it builds, which is what covers an outbound dial's
own agent. The build's copies are deleted.

`tests/unit/test_build_takes_accepted_agent.py` holds this the way the
sentinel checks above hold the rest: the entry's signature, every call to it
in `src` passing `agent=`, `AcceptedAgent(` built only in agent acceptance,
and no rule function called from `orchestrator/`. The two rules of this ADR
are unchanged: the sentinel is still the inline agent's `CallContext.agent_id`,
and `dynamic_config` is still stored raw.
