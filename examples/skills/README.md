# Skills Example

A mobile carrier's support agent built from three **skills** instead of one big
prompt. The agent's prompt lists only each skill's name and when to use it; the
model loads a skill with the built-in `load_skill` when the conversation needs
it, and that skill's instructions and tools join the conversation without
clearing it.

| Skill | Tool | When the model loads it |
|---|---|---|
| `verify_identity` | `verify_customer` | before anything about the caller's account |
| `explain_bill` | `get_invoices` | a verified caller asks why a bill changed |
| `payment_promise` | `create_payment_promise` | a verified caller wants to pay later |

`max_loaded_skills` is 2, so loading the third unloads the one loaded longest
ago. No phone number or tunnel needed: talk to it in text over the Chat API, or
by voice in the browser.

## Run it

```bash
make docker-up-local && make migrate-local

# terminal 1: the tools' webhook server (a mock CRM)
cd examples/skills && uvicorn webhook_server:app --host 0.0.0.0 --port 9100

# terminal 2
./examples/skills/run.sh                    # OpenAI LLM + Deepgram speech
./examples/skills/run.sh --provider xai     # Grok for STT, LLM and TTS (XAI_API_KEY)
```

The tools' URLs default to `http://host.docker.internal:9100`, which is the
host as the docker stack sees it. With TurnCall running on the host
(`make run`), pass `--webhook-url http://localhost:9100`.

## Talk to it

Text, using the command setup prints:

```bash
.venv/bin/python examples/skills/chat.py --api-key tc_... --agent-id <uuid>
```

Voice: `./examples/webrtc-client/run.sh`, then Server URL
`http://localhost:8090` with the same key and agent id.

The mock CRM knows two callers: last 4 digits **4471**, born **1985-03-03**
(Jane Doe, whose October bill rose by $22), and **2210**, born **1990-07-14**.

```text
you> Hi, my bill went up this month and I don't know why.
   [tool] load_skill({"name": "verify_identity"})
maya> To help with your bill, could you give me your date of birth and the last four digits of your account number?
you> Sure, I was born March 3rd 1985 and the last four are 4471.
   [tool] verify_customer({"dob": "1985-03-03", "last4": "4471"})
   [tool] load_skill({"name": "explain_bill"})
   [tool] get_invoices({"account_id": "ACC-4471"})
maya> Your bill rose by $22 this month due to the $12 tariff adjustment on Unlimited plans and a $10 late fee ...
you> OK. I can't pay all of it by the 15th though, can I pay on the 25th instead?
   [tool] load_skill({"name": "payment_promise"})     <- unloads verify_identity (the limit is 2)
maya> I can set up a payment promise for the full $70 by 25 November. Does that sound good?
```

## What to look at

- `GET /v1/chat/sessions/{id}/tool-invocations`: each `load_skill` result,
  including `unloaded` and `already_loaded`.
- On a voice call, `GET /v1/calls/{id}` has `skills` (each resolved version and
  whether it was loaded), and `GET /v1/calls/{id}/events?event_type=skill.loaded`
  lists the loads.
- `GET /v1/skills/verify_identity/agents`: who uses the skill, on which version.

## Try next

- **Change a skill for every agent at once:** `POST /v1/skills` with
  `"name": "verify_identity"` makes draft v2; publish it, and the agent's next
  conversation runs v2 with no agent publish. `POST /v1/skills/{v1 id}/rollback`
  goes back.
- **Pin a version:** give the agent's entry a `version_id`
  (`{"skill": "verify_identity", "version_id": "<v1 id>"}`) and it keeps that
  version whatever is published later.
- **Delete a skill in use:** `DELETE /v1/skills/{id}` answers 409 naming the agent.

See `docs/guides/skills.mdx` for the full reference.
