# 0011 — Project deletion (`DELETE /v1/projects/{id}`)

**Status: Accepted** — decisions below (was: scoping proposal).

## Decisions (chosen)

1. **Soft delete** — add `projects.deleted_at`. The rows stay (call
   history/analytics/config preserved, purge job reclaims later). Enforcement is
   at the **auth boundary**: a key whose project is soft-deleted stops
   authenticating, which disables the whole project (no key works → no API, no
   calls) without filtering `deleted_at` into every query. `get_project` 404s a
   soft-deleted project.
2. **Best-effort storage sweep now** — KB document files + call recordings are
   deleted from storage at delete time (best-effort, logged). **Caveat:** this
   makes the soft delete *not* fully undoable — the DB metadata survives but the
   blobs don't. Deliberate: reclaim the large/expensive storage immediately,
   keep the cheap queryable history.
3. **Auto-unbind then delete** — the endpoint unbinds each bound phone number
   (clearing Twilio) before soft-deleting; it owns the partial-failure handling
   (a Twilio failure mid-unbind → 502, nothing soft-deleted yet).

**Follow-up (separate):** a purge job that hard-cascade-deletes soft-deleted
projects past a retention window; `TurnCallClient.delete_project` + builder
rollback/teardown wiring.

## Context

TurnCall can `POST` and `GET` projects but never delete one. This surfaced from
the builder's create-agent saga (ADR-adjacent, builder PR #10): the builder
provisions a dedicated TurnCall **project per agent**, and on a failed create it
can roll back the *agent* (`delete_agent`) but **not the project + key** — there
is no endpoint. Normal agent teardown has the same gap: archiving the agent
leaves its project behind. So empty/orphaned projects accumulate in TurnCall.

A project delete would close both: saga rollback and normal teardown cleanup.

### What a project owns

Every `project_id` foreign key is `ondelete="CASCADE"`, and the second level
cascades from its parent, so a single `DELETE FROM projects WHERE id = …`
removes the entire tree at the DB layer:

```
project
├── api_keys
├── agents ── agent_knowledge_bases
├── phone_numbers
├── calls ── call_events, tool_invocations
├── webhook_subscriptions
├── test_suites ── test_runs
├── sms_sessions ── sms_messages
├── knowledge_bases ── documents ── document_chunks
└── takeaways
```

### What the DB cascade does NOT clean (the real design surface)

1. **Object storage** — KB document files (`kb/{kb_id}/docs/…`) and call
   recordings (`recordings/{call_id}.wav`) live in S3/local storage. The FK
   cascade never touches them → orphaned blobs.
2. **Twilio** — phone numbers bound to Twilio keep their webhooks pointed at
   TurnCall. Deleting the row doesn't clear Twilio → a live number dialing a
   dead binding.
3. **Irreversibility** — there is no soft-delete/archive precedent for projects
   (agents use `archived`; projects have no `deleted_at`). A cascade delete is a
   hard, unrecoverable destruction of all call history + analytics for the
   project.

## Proposed decision

### Authorization — self-delete only

`DELETE /v1/projects/{id}` authenticated by a key belonging to that project,
`AdminAuth` (admin role). The path `{id}` must equal `auth.project_id` — a
project can only delete **itself**, consistent with the whole API being
project-scoped (no cross-project access). The builder holds each agent-project's
admin key, so it can call this for rollback/teardown. The key cascade-deletes as
part of its own request (fine — the response is already computed).

### Safety guard — refuse while Twilio numbers are bound

Mirror the builder's existing agent-delete guard: if the project has any
`phone_numbers` with active routing, **refuse with 409** ("unbind N number(s)
first"). This makes the caller run the existing unbind path — which *does* clear
Twilio — so the endpoint never has to talk to Twilio itself, and a live number
can't be stranded. (Saga rollback isn't affected: rollback happens before any
bind.)

### Storage cleanup — best-effort sweep before the DB delete

Enumerate the project's KB document `storage_key`s and call recording keys,
delete them from the storage adapter (best-effort, logged on failure), *then*
issue the DB cascade delete. Best-effort so a storage hiccup can't block the
delete or leave the DB half-torn; orphaned blobs are logged, not fatal.

### Hard delete, 404 on missing

Hard cascade (no soft-delete — the point is cleanup, and there's no archive
precedent). `404` if the project doesn't exist (matches `get_project`); the
delete is otherwise idempotent-ish (a second call 404s).

## Consequences

- Destroys all call history/analytics for the project — **irreversible**. The
  409-if-bound guard + admin-only + self-scoped auth are the safety rails.
- Builder gains complete rollback + teardown; `DELETE /agents/{id}` in the
  builder can follow up by deleting the now-empty project.
- One new client method (`TurnCallClient.delete_project`) + wiring in the
  builder's rollback and agent-teardown paths (separate builder PR).

## Open questions (need decisions)

1. **Hard vs soft delete.** Hard cascade (proposed) is simplest and matches the
   cleanup goal but is unrecoverable. A soft `deleted_at` would preserve call
   history and allow undo, at the cost of every project-scoped query filtering
   it out + a real purge job later. For a localhost-builder-driven platform,
   hard delete is likely fine — but this is the call to confirm.
2. **Storage cleanup scope.** Best-effort sweep now (proposed), or ship
   DB-cascade-only first and treat storage cleanup as a fast-follow? The sweep
   adds enumerate-then-delete work across KB docs + recordings.
3. **Bound-numbers behavior.** Refuse-with-409 (proposed, safe) vs.
   auto-unbind-then-delete (fewer steps for the caller, but the endpoint then
   owns Twilio failure modes mid-delete).
```
