"""Time to first token on a Claude agent, prompt caching off vs on (#374).

Usage:
    ANTHROPIC_API_KEY=sk-ant-... uv run python scripts/measure_anthropic_cache.py \
        [--model claude-sonnet-5] [--turns 6] [--rounds 3]

The LLM is built as a call builds it (`pipeline_factory._create_llm_service`),
and each turn sends the request pipecat's `AnthropicLLMService` sends — its
system block, tools, cache breakpoints, thinking default and beta — streamed,
timed to the first content block as pipecat's TTFB is. "off" is the same
service with `enable_prompt_caching=False`. Every conversation gets a fresh
nonce at the head of its system prompt, so none reads another's cache: "on"
writes on turn 1 and reads from turn 2, which is what one call does.

Costs real money: rounds x 2 x turns requests on a ~2K-token prefix (prompt + tools).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext

from turncall.domain.models import AgentConfig, LLMConfig
from turncall.orchestrator.pipeline_factory import _create_llm_service
from turncall.services.credentials import Credentials

_LOCATIONS = ["Downtown", "Riverside", "Northgate", "Lakeview", "Old Town", "Airport"]
_SERVICES = [
    ("cleaning and exam", 45, "$120", "every six months for most patients"),
    ("whitening", 60, "$350", "not for patients under 16 or pregnant patients"),
    ("filling", 60, "$180-$260", "price depends on the number of surfaces"),
    ("crown", 90, "$1,100", "two visits, the second three weeks after the first"),
    ("root canal", 90, "$900-$1,400", "referred to Riverside when molar"),
    ("extraction", 45, "$200-$450", "surgical extractions are Northgate only"),
    ("emergency visit", 30, "$95", "same-day slots held until 11am each day"),
]


def _system_prompt(nonce: str) -> str:
    """A receptionist's prompt with a prompt-mode KB's worth of facts: about
    1.5K tokens, as an agent with a small FAQ attached carries."""
    lines = [
        f"Session {nonce}.",
        "You are Maya, the phone receptionist for Brightsmile Dental, a group of",
        "six clinics. You speak with callers on the phone, so keep every reply to",
        "one or two short sentences, never use lists, markdown or emoji, and say",
        "numbers the way a person would say them aloud. Ask one question at a",
        "time. Confirm a booking back to the caller before you make it, and never",
        "give clinical advice: offer an appointment instead. If the caller is in",
        "severe pain, has swelling that affects breathing or swallowing, or",
        "bleeding that will not stop, tell them to call emergency services now.",
        "If the caller asks for a person, transfer them to the front desk.",
        "",
        "Clinics:",
    ]
    for i, name in enumerate(_LOCATIONS):
        lines += [
            f"- {name}: open Monday to Friday 8am to {6 + i % 2}pm and Saturday "
            f"9am to {1 + i % 3}pm, closed Sunday. Parking: "
            f"{'free lot behind the building' if i % 2 else 'street parking only'}. "
            f"Dentists: Dr. {['Patel', 'Okafor', 'Lindqvist', 'Moreau', 'Tanaka', 'Silva'][i]}"
            f" and Dr. {['Nguyen', 'Kowalski', 'Haddad', 'Brennan', 'Ito', 'Costa'][i]}."
            " Wheelchair accessible, with a step-free entrance on the side street.",
        ]
    lines += ["", "Services (duration in minutes, self-pay price, notes):"]
    for name, minutes, price, note in _SERVICES:
        for loc in _LOCATIONS:
            lines.append(f"- {name} at {loc}: {minutes} min, {price}; {note}.")
    lines += [
        "",
        "Insurance: we are in network with Delta Dental, Cigna, MetLife, Aetna",
        "and Guardian. Out-of-network patients pay at the visit and we send the",
        "claim for them. Payment plans are available for anything over $500,",
        "over six or twelve months, with no interest when paid on time.",
        "Cancellation: please give 24 hours' notice; a missed appointment",
        "without notice is charged $50, waived once per patient per year.",
        "New patients: arrive 15 minutes early, bring photo ID and an insurance",
        "card, and fill in the health form we text after booking.",
    ]
    return "\n".join(lines)


def _tools() -> ToolsSchema:
    def fn(name: str, description: str, props: dict, required: list[str]) -> Any:
        return FunctionSchema(
            name=name, description=description, properties=props, required=required
        )

    return ToolsSchema(
        standard_tools=[
            fn(
                "check_availability",
                "Find open appointment slots at a clinic for a service.",
                {
                    "clinic": {"type": "string", "enum": _LOCATIONS},
                    "service": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                ["clinic", "service"],
            ),
            fn(
                "book_appointment",
                "Book a slot the caller confirmed.",
                {
                    "slot_id": {"type": "string"},
                    "patient_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "new_patient": {"type": "boolean"},
                },
                ["slot_id", "patient_name", "phone"],
            ),
            fn(
                "cancel_appointment",
                "Cancel an existing appointment.",
                {"appointment_id": {"type": "string"}, "reason": {"type": "string"}},
                ["appointment_id"],
            ),
            fn(
                "lookup_patient",
                "Find a patient record by phone number or name and date of birth.",
                {
                    "phone": {"type": "string"},
                    "name": {"type": "string"},
                    "date_of_birth": {"type": "string"},
                },
                [],
            ),
            fn(
                "end_call",
                "End the call after saying goodbye.",
                {"reason": {"type": "string"}},
                [],
            ),
            fn(
                "transfer_call",
                "Transfer the caller to a phone number.",
                {"target_number": {"type": "string"}, "reason": {"type": "string"}},
                ["target_number"],
            ),
        ]
    )


_CALLER = [
    "Hi, I'd like to know if you do teeth whitening.",
    "How much is it, and how long does it take?",
    "Is the Lakeview clinic open on Saturdays?",
    "Do you take Cigna?",
    "What happens if I need to cancel?",
    "Okay, and is there parking at Lakeview?",
    "Do I need to bring anything as a new patient?",
    "Great, thanks. That's all for now.",
]


async def _turn(service: Any, context: LLMContext) -> tuple[float, dict, str]:
    """One request as `AnthropicLLMService._process_context` builds it."""
    params: dict[str, Any] = {
        "model": service._settings.model,
        "max_tokens": service._settings.max_tokens,
        "stream": True,
    }
    params.update(await service._get_llm_invocation_params(context))
    params.update(service._settings.extra)
    service._maybe_apply_thinking_default(params)
    params["betas"] = ["interleaved-thinking-2025-05-14"]

    started = time.perf_counter()
    stream = await service._client.beta.messages.create(**params)
    ttfb: float | None = None
    usage: dict[str, int] = {}
    text: list[str] = []
    async for event in stream:
        if event.type == "message_start":
            u = event.message.usage
            usage = {
                "input": u.input_tokens or 0,
                "cache_write": u.cache_creation_input_tokens or 0,
                "cache_read": u.cache_read_input_tokens or 0,
            }
        if ttfb is None and event.type in (
            "content_block_start",
            "content_block_delta",
        ):
            ttfb = time.perf_counter() - started
        if event.type == "content_block_delta" and hasattr(event.delta, "text"):
            text.append(event.delta.text)
    return (ttfb or 0.0) * 1000, usage, "".join(text)


async def _conversation(model: str, key: str, caching: bool, turns: int) -> list:
    config = AgentConfig(llm=LLMConfig(provider="anthropic", model=model))
    service = _create_llm_service(
        config,
        Credentials(platform={"anthropic": key}),
        system_instruction=_system_prompt(uuid.uuid4().hex),
    )
    service._settings.enable_prompt_caching = caching
    context = LLMContext(tools=_tools())
    rows = []
    for said in _CALLER[:turns]:
        context.add_message({"role": "user", "content": said})
        ms, usage, reply = await _turn(service, context)
        context.add_message({"role": "assistant", "content": reply or "Okay."})
        rows.append((ms, usage))
    return rows


def _summary(label: str, runs: list[list]) -> None:
    first = [r[0][0] for r in runs]
    later = [ms for r in runs for ms, _ in r[1:]]
    reads = [u["cache_read"] for r in runs for _, u in r[1:]]
    writes = [u["cache_write"] for r in runs for _, u in r]
    inputs = [u["input"] for r in runs for _, u in r[1:]]
    print(
        f"{label:>4}: turn 1 median {statistics.median(first):6.0f} ms | "
        f"turns 2+ median {statistics.median(later):6.0f} ms "
        f"(p90 {sorted(later)[int(0.9 * (len(later) - 1))]:.0f}, n={len(later)}) | "
        f"turns 2+ uncached input median {statistics.median(inputs):.0f} | "
        f"cache_read median {statistics.median(reads):.0f} | "
        f"cache_write total {sum(writes)}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    results: dict[str, list] = {"off": [], "on": []}
    for i in range(args.rounds):
        # Alternate the order so neither arm always runs on a warmer link.
        order = ("off", "on") if i % 2 == 0 else ("on", "off")
        for arm in order:
            rows = await _conversation(args.model, key, arm == "on", args.turns)
            results[arm].append(rows)
            print(
                f"round {i + 1} {arm:>3}: "
                + "  ".join(
                    f"{ms:.0f}ms(r{u['cache_read']}/w{u['cache_write']}/i{u['input']})"
                    for ms, u in rows
                )
            )
    print(f"\nmodel {args.model}, {args.rounds} conversations x {args.turns} turns")
    _summary("off", results["off"])
    _summary("on", results["on"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
