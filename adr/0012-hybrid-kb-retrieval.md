# 0012 — Hybrid KB retrieval: RRF fusion, contextual enrichment, query windowing

KB retrieval was pure vector cosine over pypdf chunks, queried with the last
user utterance alone. On record-like documents (reservations, orders) it
failed the questions voice callers actually ask: "what is the flight date?"
scored 0.34 against the chunk that literally contains the date, follow-ups
("and what time?") carried no signal at all, and print-to-PDF chrome drowned
the content. Constraint: auto-mode retrieval sits on the audio path, so
**no per-turn LLM calls** (query rewriting, reranking) are allowed.

## Decisions

- **Hybrid search, RRF-fused.** One SQL query runs vector KNN and Postgres
  full-text (`websearch_to_tsquery`, generated `tsvector` column + GIN index)
  side by side, combined by Reciprocal Rank Fusion (`1/(60+rank)`). Chosen over
  score-weighted blending because RRF has no weights to tune across
  incomparable scales, and over a reranker because of the latency budget.
  The similarity threshold applies to the vector leg only — an exact lexical
  match is evidence enough. If query embedding fails (no OpenAI key, outage),
  retrieval degrades to lexical-only instead of erroring.

- **Contextual enrichment at ingest (never at query time).** Each chunk is
  stored as `[<filename> — <LLM situating context>]\n<chunk>`, so embeddings
  and tsvector both carry document identity ("Bangkok Airways reservation for
  Jacob Kisos, July 2026") rather than bare fragment text. gpt-4o-mini, one
  call per chunk, capped (first 60 chunks, 24k doc chars); every failure path
  degrades to a filename-only prefix — enrichment never blocks or fails an
  upload. Chosen over filename-prefix-only for retrieval quality on record
  documents, accepting slower/costlier ingest.

- **Windowed retrieval query.** Auto-mode queries are built from the previous
  user turn + the agent's last reply + the current utterance (recency last,
  600-char cap), mined from the message history already in the frame. Voice
  follow-ups are anaphoric; the window carries the entities they refer to.
  Chosen over LLM query rewriting (latency) and over single-utterance
  (misses every follow-up).

- **Golden-set eval, not vibes.** `scripts/eval_retrieval.py` runs
  (query → expected substring) cases against a live KB and reports hit@k +
  MRR; `scripts/rag_golden.yaml` holds the reservation-document fixture.
  Every future RAG change gets a before/after number.

Kept: `text-embedding-3-small` (per-KB `embedding_model` remains the
escalation path), 512-token chunks, threshold 0.3 (ADR-0011-era fix).
Existing documents keep their embeddings — the tsvector column backfills via
migration; re-upload (console "Replace") re-ingests through enrichment.
