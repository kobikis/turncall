"""Sync the turncall-skill repo's API snapshot from this repo's docs/openapi.json.

The turncall-skill plugin bundles a copy of openapi.json as its field-exact API
reference. It's a snapshot of docs/openapi.json, so it drifts whenever the API
changes. This copies the current spec into a local turncall-skill checkout.

Invariant: the skill's openapi.json is byte-identical to docs/openapi.json here.
Run `make gen-openapi` first (or use `make sync-skill`, which does) so docs is
current before it's copied.

`API.md` in the skill is hand-curated prose (not derivable from the spec) and is
NOT touched — update it by hand when endpoints are added/removed.

Usage:
    python scripts/sync_skill.py --skill-repo ../turncall-skill
    python scripts/sync_skill.py --skill-repo ../turncall-skill --check   # CI guard
    # skill repo also resolvable via TURNCALL_SKILL_REPO; defaults to ../turncall-skill
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "docs" / "openapi.json"
SKILL_REL = Path("skills/turncall/openapi.json")


def _resolve_skill_repo(arg: str | None) -> Path:
    raw = arg or os.environ.get("TURNCALL_SKILL_REPO") or str(REPO.parent / "turncall-skill")
    return Path(raw).expanduser().resolve()


def _schema_count(text: str) -> int:
    try:
        return len(json.loads(text).get("components", {}).get("schemas", {}))
    except Exception:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill-repo",
        default=None,
        help="Path to the turncall-skill checkout (default: ../turncall-skill "
        "or $TURNCALL_SKILL_REPO)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the skill snapshot differs from docs/openapi.json",
    )
    args = parser.parse_args()

    if not SOURCE.exists():
        print(f"missing {SOURCE} — run `make gen-openapi` first.", file=sys.stderr)
        return 1

    target = _resolve_skill_repo(args.skill_repo) / SKILL_REL
    if not target.parent.exists():
        print(
            f"skill snapshot dir not found: {target.parent}\n"
            "Pass --skill-repo <path> or set TURNCALL_SKILL_REPO.",
            file=sys.stderr,
        )
        return 1

    source_text = SOURCE.read_text()
    target_text = target.read_text() if target.exists() else ""

    if source_text == target_text:
        print(f"{target} is up to date ({_schema_count(source_text)} schemas).")
        return 0

    if args.check:
        print(
            f"{target} is out of date "
            f"(skill {_schema_count(target_text)} vs docs {_schema_count(source_text)} "
            "schemas) — run `make sync-skill`.",
            file=sys.stderr,
        )
        return 1

    target.write_text(source_text)
    print(
        f"synced {target}\n"
        f"  schemas: {_schema_count(target_text)} -> {_schema_count(source_text)}\n"
        f"  API.md is hand-curated — review it if endpoints changed.\n"
        f"  next: cd {target.parents[2]} && git checkout -b chore/refresh-api-snapshot "
        f"&& git add {SKILL_REL} && git commit && gh pr create"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
