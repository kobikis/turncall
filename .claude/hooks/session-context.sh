#!/bin/bash
# Hook 7: Re-inject critical project context after compaction
cat << 'CONTEXT'
=== CRITICAL CONTEXT (re-injected after compaction) ===

PROJECT: AI-powered receptionist service (FastAPI + VAPI + Claude)
WORKDIR: ~/PycharmProjects/receptionist-service
BRANCH:  feature/init → PRs to develop

TECH STACK:
- Python 3.12, FastAPI, AsyncPG, SQLAlchemy 2.0 async
- VAPI (voice AI), Twilio (telephony/recording), OpenAI Whisper (transcription)
- AWS S3, PostgreSQL

CONVENTIONS:
- Run: uvicorn main:app --reload --host 0.0.0.0 --port 3000
- Tests: pytest tests/ -v  (80%+ coverage required)
- Linting: ruff check --fix && black .
- VAPI error format: {"error": True, "message": "..."} — NOT results array
- Immutable patterns — never mutate objects in-place
- Files <800 lines, functions <50 lines

KEY FILES:
- app/api/v1/vapi.py — VAPI webhooks
- app/api/v1/twilio.py — Twilio webhooks + recording
- app/services/transfer_message_service.py — routing
- app/services/enriched_customer_service.py — caller ID
- app/utils/settings.py — Pydantic config
- tests/conftest.py — fixtures

MCP: cmem (memory), atlassian (Jira cloud: d1834268-fed8-470c-961a-d342b1b679c2)

=== END CONTEXT ===
CONTEXT
