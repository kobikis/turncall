"""Skills example — talk to the agent over the Chat API, one line per message.

Prints each reply and the tools the turn called, so a `load_skill` shows up
as it happens. Interactive, or piped: `printf 'hi\\nbye\\n' | python chat.py ...`

Usage:
  python examples/skills/chat.py --api-key tc_... --agent-id <uuid>
"""

import argparse
import json
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the skills agent")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--base-url", default="http://localhost:8090")
    args = parser.parse_args()

    client = httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {args.api_key}"},
        timeout=120,
    )
    session_id = None
    seen: set[str] = set()
    interactive = sys.stdin.isatty()

    while True:
        line = input("you> ") if interactive else sys.stdin.readline()
        if not line:
            break
        message = line.strip()
        if not message:
            continue
        if not interactive:
            print(f"you> {message}")

        body = {"agent_id": args.agent_id, "message": message}
        if session_id:
            body["session_id"] = session_id
        resp = client.post("/v1/chat", json=body)
        if resp.status_code >= 400:
            sys.exit(f"ERROR {resp.status_code}: {resp.text}")
        data = resp.json()["data"]
        session_id = data["session_id"]

        calls = client.get(f"/v1/chat/sessions/{session_id}/tool-invocations")
        for call in sorted(calls.json()["data"], key=lambda c: c["created_at"]):
            if call["id"] not in seen:
                seen.add(call["id"])
                print(
                    f"   [tool] {call['tool_name']}({json.dumps(call['input_json'])})"
                )
        print(f"maya> {data['reply']}")

    if session_id:
        print(f"\nsession {session_id}")


if __name__ == "__main__":
    main()
