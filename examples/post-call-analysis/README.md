# Post-Call Structured Analysis

Automatically analyze completed calls with LLM-powered structured extraction.

## What It Does

When a call ends, TurnCall runs background analysis (~2-5s) and includes the results in the `call.ended` webhook event:

- **Summary** — Concise call summary (customizable prompt)
- **Success Evaluation** — Pass/fail, 1-5 likert, or 0-100 numeric
- **Sentiment** — Customer sentiment and satisfaction
- **Structured Extraction** — Extract fields via JSON Schema (e.g., issue, resolution, product)
- **Scoring Rubric** — Score on custom criteria (e.g., professionalism, problem-solving)

## call.ended Event

All data is included in a single comprehensive `call.ended` webhook:

```json
{
  "event": "call.ended",
  "call_id": "...",
  "payload": {
    "from_number": "+15551234567",
    "to_number": "+15559876543",
    "direction": "inbound",
    "duration_ms": 51000,
    "started_at": "...",
    "ended_at": "...",
    "transcript": [
      {"role": "customer", "text": "Hi. Good morning.", "timestamp": "..."},
      {"role": "agent", "text": "Good morning! How can I assist you?", "timestamp": "..."}
    ],
    "recording_url": "./storage/recordings/{call_id}/{recording_sid}.wav",
    "summary": "Customer called about a billing issue...",
    "analysis": {
      "summary": "...",
      "success_evaluation": {"score": "pass", "reason": "Issue resolved"},
      "sentiment": {"overall": "positive", "customer_satisfaction": "satisfied"},
      "structured_data": {
        "customer_issue": "billing",
        "resolution": "credited account"
      },
      "scoring": {
        "professionalism": {"score": 9, "reason": "Very courteous"},
        "problem_solving": {"score": 8, "reason": "Quick resolution"}
      },
      "model": "gpt-4o-mini",
      "duration_ms": 2350
    }
  }
}
```

## Setup

```bash
python examples/post-call-analysis/setup.py \
  --twilio-number-sid PN_YOUR_SID \
  --twilio-number +15551234567
```

## API

```bash
# Get analysis results
GET /v1/calls/{call_id}/analysis

# Re-run analysis
POST /v1/calls/{call_id}/analysis/rerun
```

## Agent Config

```json
{
  "analysis": {
    "enabled": true,
    "summary_enabled": true,
    "summary_prompt": "Summarize in 2-3 sentences...",
    "success_evaluation": {
      "enabled": true,
      "rubric": "Was the issue resolved?",
      "scale": "pass_fail"
    },
    "sentiment_enabled": true,
    "structured_extraction_schema": {
      "type": "object",
      "properties": {
        "customer_issue": {"type": "string"},
        "resolution": {"type": "string"},
        "follow_up_needed": {"type": "boolean"}
      }
    },
    "scoring_rubric": {
      "professionalism": {"max_score": 10, "description": "..."},
      "problem_solving": {"max_score": 10, "description": "..."}
    },
    "model": "gpt-4o"
  }
}
```

## Scale Options

| Scale | Values | Use Case |
|-------|--------|----------|
| `pass_fail` | "pass" / "fail" | Simple success check |
| `likert` | 1-5 | Customer satisfaction |
| `numeric` | 0-100 | Detailed scoring |

## Quick run

```bash
./run.sh
```

Reads `TURNCALL_NUMBER`, `TWILIO_PN_SID` from the environment or the repo-root `.env`; extra args pass through to `setup.py`.
