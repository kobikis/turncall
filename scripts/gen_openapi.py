"""Regenerate docs/openapi.json from the FastAPI app — the source of truth.

The committed docs/openapi.json feeds the docs site and is snapshotted into the
turncall-skill repo. It has no other generator, so it drifts unless refreshed;
run this whenever the API (routes or Pydantic schemas) changes.

Usage:
    python scripts/gen_openapi.py            # write docs/openapi.json
    python scripts/gen_openapi.py --check    # exit 1 if stale (for CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from turncall.app import create_app

OUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render() -> str:
    """Deterministic openapi.json text: the app spec, indent=2, trailing newline."""
    spec = create_app().openapi()
    return json.dumps(spec, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/openapi.json is out of date instead of writing it",
    )
    args = parser.parse_args()

    new = render()

    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != new:
            print(
                f"{OUT} is out of date — run `make gen-openapi` and commit.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT} is up to date.")
        return 0

    OUT.write_text(new)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
