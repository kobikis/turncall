# ADR-0030: Skills follow the live version — a published agent can change

**Status:** Accepted
**Date:** 2026-10-29
**Relates to:** ADR-0017 (calls without an agent row), ADR-0024 (compliance
kit: audit log, retention). PRD #421; #424, #425, #426.

## Context

Agent versioning makes one promise: a published agent version is immutable.
Editing means a new draft; what a published version does on a call is fixed
when it is published, and a rollback is how anything goes back.

Skills (#421) are capabilities several agents share: instructions, tools and
knowledge bases, versioned like an agent. An agent lists them by name. The
point is to fix a rule once — say, how a caller is verified — and have every
agent that verifies callers run the fix. Four agents each republished to pick
up one sentence is the drift skills exist to end.

That cannot hold together with the promise above. If an agent's skill entry
follows whatever version of the skill is live, then publishing the skill
changes what a published agent does on its next call, and nobody published the
agent.

## Decision

**An agent's skill entry follows the skill's live published version by
default.** It is resolved once per call, when the call's pipeline is built
(and again for a handoff's target): a publish or a rollback of the skill
reaches every following agent's next call, with no agent publish. A running
call never changes under it.

**An entry may pin a version** (`{"skill": "verify_identity", "version_id":
"<skill version id>"}`, #426). A pinned entry runs exactly that version,
whatever is published or rolled back later — published or **archived**, since
the next publish archives the pinned version and a pin that stopped there
would follow the live one after all. A draft cannot be pinned: it can still
be edited. The pin is the skill version's **id**, not its number: a rollback
puts an older number live again, and the id names one immutable row whatever
the name's history does next. At save (#428) a pin must name a published or
archived version of that skill in the agent's project (`422` otherwise), and
the tools checked for collisions are the pinned version's.

**This relaxes "a published agent version is immutable"** to: a published
agent version's *own config* is immutable; the skills it follows are not. An
operator who needs an agent frozen — a regulated line, a version under test —
pins its skills.

What keeps the relaxed promise explainable:

- **Each call records the versions it ran.** The resolution is stored on the
  call row (`metadata_json["resolved_skills"]`, #425) and shown as `skills` on
  the call read and `call.ended`: `{skill, version, pinned, loaded}` per entry.
  "Why did this call behave differently from yesterday's?" is answered from
  the two calls, without reconstructing what was live when.
- **Each load is an event** (`skill.loaded {skill, version, reason}`).
- **Who a change will reach is listed before it is made.**
  `GET /v1/skills/{name}/agents` lists every draft and published agent version
  naming the skill, the version each resolves to now and whether it is pinned.
- **Every skill publish, rollback and delete is in the audit log** (ADR-0024),
  so the change that altered an agent's behaviour has an author and a time.
- A skill version an agent lists cannot be deleted (`409`), pinned or not.

## Consequences

- Publishing a skill is a deploy of every agent following it. The agents list
  is the blast radius; the docs say so where publish is described.
- A pin is the only way to hold an agent on a skill version; there is no
  "pin at publish" snapshot. A later skill version whose tool collides with an
  agent's is still dropped at resolution, as before (#424).
- A pin is never moved for the operator: moving an agent to a newer version
  is an edit of the agent's config, as any agent change is.
- Old call records (before #426) carry no `pinned` and read `pinned: false`,
  which is what they were.
