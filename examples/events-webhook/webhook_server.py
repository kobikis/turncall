"""Events Webhook Server — receive and log all TurnCall events.

Starts a simple FastAPI server that receives webhook events from TurnCall
and pretty-prints them. Use this to see what events fire during a call.

Run:
    uvicorn webhook_server:app --port 9001

Then subscribe this server to TurnCall events:
    python setup.py --base-url http://localhost:8090 --webhook-url http://host.docker.internal:9001/events
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="TurnCall Events Webhook Server")

# In-memory event log
EVENT_LOG: list[dict] = []


def _verify_signature(
    payload: bytes,
    signature: str,
    secret: str,
) -> bool:
    """Verify HMAC-SHA256 webhook signature."""
    if not signature or not secret:
        return True  # Skip if no signature configured
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.post("/events")
async def receive_event(
    request: Request,
    x_turncall_signature: str = Header(default="", alias="X-TurnCall-Signature"),
) -> JSONResponse:
    """Receive and log a TurnCall webhook event."""
    body = await request.body()

    # Verify signature (optional — set WEBHOOK_SECRET in setup.py)
    # if not _verify_signature(body, x_turncall_signature, "your-secret"):
    #     return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    event = json.loads(body)
    event_type = event.get("event") or event.get("event_type", "unknown")
    call_id = event.get("call_id", "")
    payload = event.get("payload", {})

    EVENT_LOG.append(
        {
            "received_at": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "call_id": call_id,
            "payload": payload,
        }
    )

    # Pretty print
    print(f"\n{'='*70}")
    print(f"  RAW EVENT: {event}")
    print(f"  EVENT: {event_type}")
    print(f"  Call:  {call_id}")
    print(f"  Time:  {datetime.now(UTC).strftime('%H:%M:%S')}")
    print(f"{'='*70}")

    if event_type == "call.ended":
        _print_call_ended(payload)
    elif event_type == "call.started":
        print(f"  Agent: {payload.get('agent_id', 'N/A')}")
    elif event_type == "transcript.final":
        speaker = payload.get("role", "unknown")
        text = payload.get("text", "")
        label = "Agent" if speaker == "assistant" else "Customer"
        print(f"  {label}: {text}")
    elif event_type == "tool.called":
        print(f"  Tool: {payload.get('tool_name', 'N/A')}")
        print(f"  Args: {json.dumps(payload.get('arguments', {}), indent=2)}")
    elif event_type == "tool.result":
        print(f"  Tool: {payload.get('tool_name', 'N/A')}")
        print(f"  Args: {json.dumps(payload.get('arguments', {}), indent=2)}")
        print(f"  Result: {payload.get('result', 'N/A')}")
    elif event_type == "recording.ready":
        print(f"  URL: {payload.get('recording_url', 'N/A')}")
        print(f"  Duration: {payload.get('recording_duration', 'N/A')}s")
    elif event_type in ("session.created", "session.updated", "session.deleted"):
        print(f"  Session: {payload.get('session_id', call_id)}")
        print(f"  Channel: {payload.get('channel', 'N/A')}")
    else:
        if payload:
            print(f"  Payload: {json.dumps(payload, indent=2, default=str)[:500]}")

    return JSONResponse({"received": True})


def _print_call_ended(payload: dict) -> None:
    """Pretty-print the comprehensive call.ended event."""
    print(f"\n{'$'*70}")
    print(payload)
    print(f"\n{'$'*70}")
    # Transcript
    transcript = payload.get("transcript", [])
    if transcript:
        print(f"\n  --- Transcript ({len(transcript)} turns) ---")
        for entry in transcript[:20]:  # Show first 20
            role = entry.get("role", "unknown").capitalize()
            text = entry.get("text", "")
            print(f"  {role}: {text}")
        if len(transcript) > 20:
            print(f"  ... ({len(transcript) - 20} more turns)")

    # Recording
    recording_url = payload.get("recording_url")
    if recording_url:
        print(f"\n  Recording: {recording_url}")

    # Summary
    summary = payload.get("summary")
    if summary:
        print(f"\n  Summary: {summary}")

    # Analysis
    analysis = payload.get("analysis")
    if analysis:
        _print_analysis(analysis)


def _print_analysis(analysis: dict) -> None:
    """Pretty-print analysis results."""
    success = analysis.get("success_evaluation")
    if success:
        print(f"  Success: {success.get('score')} — {success.get('reason', '')}")

    sentiment = analysis.get("sentiment")
    if sentiment:
        print(
            f"  Sentiment: {sentiment.get('overall')} (satisfaction: {sentiment.get('customer_satisfaction', 'N/A')})"
        )

    structured = analysis.get("structured_data")
    if structured:
        print(f"  Extracted: {json.dumps(structured, indent=4, default=str)}")

    scoring = analysis.get("scoring")
    if scoring:
        print("  Scores:")
        for name, score_data in scoring.items():
            print(
                f"    {name}: {score_data.get('score', 'N/A')} — {score_data.get('reason', '')}"
            )

    model = analysis.get("model")
    duration = analysis.get("duration_ms")
    if model:
        print(f"  Model: {model} ({duration}ms)")


@app.get("/events")
async def list_events(limit: int = 50) -> JSONResponse:
    """List recent events (for debugging)."""
    return JSONResponse({"events": EVENT_LOG[-limit:], "total": len(EVENT_LOG)})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "events_received": len(EVENT_LOG)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9001)
