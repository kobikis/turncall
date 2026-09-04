# Knowledge Base Example

A customer support agent with three knowledge bases demonstrating all RAG retrieval modes.

## What it does

Creates an Acme Corp support agent with:

- **FAQ** (prompt mode) — small FAQ injected fully into system prompt every call
- **Product Catalog** (auto mode) — per-turn semantic search via pgvector
- **Troubleshooting Guide** (tool mode) — LLM calls `query_knowledge` when the user reports a problem

| You say | What happens |
|---------|-------------|
| "What are your hours?" | Answered from FAQ (always in prompt) |
| "Tell me about Widget Pro" | Catalog searched automatically via RAG |
| "My widget won't connect" | LLM queries troubleshooting guide on demand |

## Prerequisites

1. **OpenAI API key** (for LLM + embeddings)
2. **Deepgram API key** (for STT + TTS)
3. **Docker** (for Postgres + Redis)
4. **PostgreSQL with pgvector** extension

## Quick Start

### 1. Configure environment

```bash
cp env.example .env
# Edit .env:
#   OPENAI_API_KEY=sk-xxxxxxxx
#   DEEPGRAM_API_KEY=xxxxxxxx
```

### 2. Start infrastructure

```bash
make docker-up    # postgres + redis
make dev          # install dependencies
```

### 3. Enable pgvector and run migrations

```bash
# Connect to postgres and enable the extension:
psql postgresql://turncall:turncall@localhost:5432/turncall -c "CREATE EXTENSION IF NOT EXISTS vector;"

make migrate
```

### 4. Start the server

```bash
make run
# Server starts on http://localhost:8090
```

### 5. Run setup script

```bash
python examples/knowledge-base/setup.py
```

This creates:
- 1 project + API key
- 3 knowledge bases (FAQ, catalog, troubleshooting)
- 3 documents (uploaded and embedded)
- 1 agent with all 3 KBs linked in different modes

### 6. Test via Chat API

```bash
# General question (FAQ — prompt mode)
curl -X POST http://localhost:8090/v1/chat \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-id>", "message": "What are your business hours?"}'

# Product question (Catalog — auto mode)
curl -X POST http://localhost:8090/v1/chat \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-id>", "message": "How much does Widget Enterprise cost?"}'

# Technical issue (Troubleshooting — tool mode)
curl -X POST http://localhost:8090/v1/chat \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-id>", "message": "My widget won'\''t connect to the server"}'
```

### 7. Test search directly

```bash
curl -X POST http://localhost:8090/v1/knowledge-bases/<catalog-kb-id>/search \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "enterprise pricing", "top_k": 3}'
```

## Retrieval Modes

| Mode | When context is fetched | Best for |
|------|------------------------|----------|
| `prompt` | Once at call/chat start (full text) | Small docs (<5KB): FAQs, company info |
| `auto` | Every user turn (semantic search) | Medium docs: product catalogs, policies |
| `tool` | When LLM decides to search | Large docs: technical manuals, archives |

## Architecture

```
Document Upload Flow:
  PDF/TXT/DOCX → Extract text → Chunk (token-based) → Embed (OpenAI) → Store (pgvector)

Voice Call Flow:
  Audio → STT → user_agg → [KnowledgeRetrievalProcessor] → LLM → TTS → Audio
                             (auto mode: searches every turn)

Chat API Flow:
  Message → Session → [KB retrieval] → LLM completion → Reply
                       (prompt: full text injected)
                       (auto: semantic search on user message)

Tool Mode:
  LLM decides to call query_knowledge → Retrieval service → Results returned to LLM
```

## Files

| File | Purpose |
|------|---------|
| `setup.py` | Creates project, KBs, uploads docs, creates agent, links KBs |

## Supported File Types

PDF, TXT, Markdown, DOCX, CSV, JSON, YAML, XML, TSV. Max 10 MB per file.

## Quick run

```bash
./run.sh
```

All args pass through to `setup.py`.
