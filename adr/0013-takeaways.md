# 0013 — Takeaways: reusable post-call structured outputs

TurnCall already extracts structured data post-call via
`analysis.structured_extraction_schema` — but as one inline schema per agent:
no reuse across agents, no multiple extractions per call, no per-extraction
prompt/model. Vapi ships this as "Structured Outputs" (first-class definitions
attached by id). This ADR ports the product shape under the name **Takeaways**
— what you take away from a conversation.

## Decisions

- **Name: Takeaways.** Self-explanatory in UI copy ("add a takeaway"),
  API-friendly (`/v1/takeaways`, `analysis.takeaways`), and doesn't collide
  with the LLM-API term "structured outputs" the way Vapi's name does.

- **First-class, attach by id.** A Takeaway is a project-scoped entity
  (name, description, JSON Schema, optional prompt + model override) with CRUD
  at `/v1/takeaways`; agents attach via `analysis.takeaway_ids`. Define "CSAT
  survey" once, attach to five agents, edit in one place. Chosen over an
  inline array (no reuse — the actual gap) and over both-at-once (two code
  paths from day one).

- **One LLM call per takeaway, concurrent.** Isolated failures, per-takeaway
  model choice (cheap model for CSAT, strong for complex extraction). Chosen
  over batching all schemas into one call: one failure would poison all
  results, and merged schemas degrade extraction. Cost accepted — analysis
  already runs in the background, so N concurrent calls add money, not caller
  latency.

- **Results keyed by name** at `analysis.takeaways.<name>` in `call.ended`
  and the analysis API: `{result, valid, model, duration_ms}` (+`error` when
  invalid). Names are payload keys, so they're identifier-shaped
  (`^[a-z0-9][a-z0-9_-]*$`), unique per project, and immutable after create.

- **Validated, never silently wrong.** Extraction output is checked against
  the schema (jsonschema); one retry with the validation error; still bad →
  stored with `valid: false` + `error`. Schemas themselves are validated at
  create time, so a broken schema fails the API call, not every future call's
  analysis.

- **No new event type; inline schema stays.** Results ship inside `call.ended`
  like all analysis (ADR-0007 precedent). `structured_extraction_schema`
  keeps working unchanged (`analysis.structured_data`) — Takeaways supersede
  it in docs, not in code.

- **Deletion blocked while attached** (409 with the referencing-agent count),
  matching knowledge-base deletion semantics — a silently dead attachment
  would just stop producing data with no signal.

Scope: calls only in v1; chat sessions are a later extension. Builder console
support (a Takeaways tab) is a separate builder-repo feature.
