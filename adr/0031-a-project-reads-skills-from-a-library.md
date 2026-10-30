# ADR-0031: A project reads its skills from a library, linked by the platform

**Status:** Accepted
**Date:** 2026-10-30
**Relates to:** ADR-0011 (projects are the builder's), ADR-0024 (audit log),
ADR-0030 (skills follow the live version). PRD turncall-builder-api#130;
#432, #433, #434.

## Context

A skill (#421) belongs to one project, and an agent lists skills by name in
its own project. That is the whole of the sharing model, and it is enough for
an operator who keeps every agent in one project.

The builder does not. It mints one TurnCall project per agent, so two agents
of one workspace never share a project, and so never share a skill: the
reason skills exist — fix a rule once, every agent that uses it runs the fix —
does not reach a builder workspace at all. Merging every agent into one
project would change much more than skills (keys, numbers, limits, retention,
redaction are all per project).

A project is also TurnCall's isolation boundary. An API key reads and writes
its own project and nothing else; there is no cross-project read anywhere in
the API. Whatever lets one project use another's skills has to keep that true
for everything a key can ask for.

## Decision

**A project may be linked to one skill library: another project, whose skills
it resolves instead of its own** (`projects.skill_library_id`). One rule
decides where a project's skill names are looked up —
`services/skill_library.skill_source`: its library when linked, else itself —
and every place a skill is resolved goes through it: an agent's save checks
(unknown or draft-only names, tool collisions, pins), a call's resolution for
a stored agent, a call-init inline agent and a handoff target, a text session,
an eval run, and a skill's knowledge bases, which are checked against the
project that holds the skill.

**The library is the only source for a linked project, never a fallback.** A
linked project's own skills are not consulted, and a name the library does
not hold is not a skill there. If a local skill could shadow a library one,
or the library fill in for a missing local one, the same name would mean
different things in two agents of one workspace, and nobody could tell which
ran without reading each call's record. One place per project, decided by one
link, keeps "what does `verify_identity` mean here?" a question with one
answer.

**Only the platform credential sets or clears the link**
(`PUT /v1/projects/{id}/skill-library`, `X-Platform-Key`). An API key, admin
included, cannot: a link is a cross-project read, and a key that could point
its project at any other project id could read that project's skills —
instructions, tool URLs — on its own say-so. The platform credential is held
by the builder, which creates both projects and so already speaks for both
(ADR-0011). The link is an audit entry on the linked project by
`actor: platform` (ADR-0024), and the project read shows it.

**A library is one level deep.** A project linked to a library cannot be one,
and a project others read from cannot be linked; both are a `422`, as are the
project itself and a project that is missing or soft-deleted. Changes to links
are serialized (an advisory lock), so two links checked at once cannot form a
chain either. With no chains, "where do this project's skills come from" is
one read, never a walk.

**A library that goes away is a configuration mistake, not an outage.** A
soft-deleted library resolves nothing: the linked project's skills are a
warning and a skipped skill on a call, as a stale name always was, and a save
listing one is a `422` naming the library. A purged library unlinks its
projects (`ON DELETE SET NULL`), which then read their own skills again.

**What a library's skills are used by spans its readers.** A skill's delete is
refused (`409`) while a draft or published agent version in the library **or
in any project linked to it** lists the name, and
`GET /v1/skills/{name}/agents` lists those agents with their `project_id`.
Showing a linked project's agent names to the library's key is acceptable
because only the platform could have linked them.

## Consequences

- A builder workspace gets one library project and links each agent project
  to it; TurnCall's per-agent-project model is unchanged.
- Nothing about a call's record changes: resolved versions, `skill.*` events
  and `skills` on the call read already carry the skill's id and version from
  wherever it resolved.
- A project's own skills are inert while it is linked. They are not deleted,
  and become its source again when the link is cleared.
- A pin resolves only to a version in the source; a version of the project's
  own skill is refused at save and skipped at resolution.
- A link change applies to the next call, message or save. A call already
  running keeps the versions it resolved; a text session re-reads the versions
  it stored by id, so one that began before the change skips them after it.
- The lock a skill delete and an agent save share is taken in the project that
  holds the skill (the library), so a save in a linked project and a delete in
  the library still serialize.
