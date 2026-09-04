"""Golden-set retrieval eval: hit@k + MRR against a live knowledge base.

Usage:
    DATABASE_URL=postgresql+asyncpg://turncall:turncall@localhost:5432/turncall \
    OPENAI_API_KEY=sk-... \
    python scripts/eval_retrieval.py --kb <kb_id> [--golden scripts/rag_golden.yaml] [--top-k 5]

Each golden case is {query, expect} — a retrieval hits when any returned chunk
contains `expect` (case-insensitive). Run before and after a RAG change; the
numbers, not vibes, decide whether it shipped an improvement.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from uuid import UUID

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from turncall.services.retrieval import retrieve


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="knowledge base id")
    ap.add_argument("--golden", default="scripts/rag_golden.yaml")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    cases = yaml.safe_load(open(args.golden))["cases"]
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    hits = 0
    rr_total = 0.0
    async with sessions() as session:
        for case in cases:
            result = await retrieve(
                session,
                query=case["query"],
                knowledge_base_ids=[UUID(args.kb)],
                top_k=args.top_k,
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            )
            rank = next(
                (
                    i
                    for i, c in enumerate(result.chunks, 1)
                    if case["expect"].lower() in c.content.lower()
                ),
                0,
            )
            hits += 1 if rank else 0
            rr_total += 1.0 / rank if rank else 0.0
            mark = f"hit@{rank}" if rank else "MISS "
            print(f"  {mark:6}  {case['query'][:60]!r}  (expect {case['expect'][:30]!r})")

    n = len(cases)
    print(f"\nhit@{args.top_k}: {hits}/{n} ({hits / n:.0%})   MRR: {rr_total / n:.3f}")
    await engine.dispose()
    return 0 if hits == n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
