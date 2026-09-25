"""Fail when the API Reference nav and the OpenAPI spec disagree.

`docs/docs.json` lists every reference page **explicitly**, by method and path.
So an endpoint that ships is published only if someone remembers to add it —
and nobody did, for twelve eval routes, `DELETE /v1/projects/{id}`, the
authenticated key route and the chat tool-invocations list. The reference read
as complete the whole time, which is the failure mode worth a guard: a missing
page looks exactly like a feature nobody wrote.

Both directions matter. A page naming a path the app no longer serves is a 404
in the docs site, which is louder but rarer.

`/webhooks/` is excluded by design: Twilio and Meta call those, so they are the
platform's plumbing rather than its API. `docs/api-reference/overview.mdx` says
so out loud, which is what keeps this exclusion from reading as an oversight.

Usage:
    python scripts/check_docs_nav.py    # exit 1 on any disagreement
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

# Called by a provider, never by a customer. See the overview page.
EXCLUDED_PREFIXES = ("/webhooks/",)


def nav_entries(node: object) -> set[str]:
    """Every "METHOD /path" string anywhere in the navigation tree.

    Walked rather than indexed by key: the tab/group shape is Mintlify's and
    has already changed once, and this check is about the set of endpoints, not
    about where they sit.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            found |= nav_entries(value)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, str) and value.split(" ")[0] in METHODS:
                found.add(value)
            else:
                found |= nav_entries(value)
    return found


def spec_entries(spec: dict) -> set[str]:
    return {
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if not path.startswith(EXCLUDED_PREFIXES)
    }


def main() -> int:
    spec = json.loads((DOCS / "openapi.json").read_text())
    docs = json.loads((DOCS / "docs.json").read_text())

    in_spec = spec_entries(spec)
    in_nav = nav_entries(docs)

    unpublished = sorted(in_spec - in_nav)
    dangling = sorted(
        in_nav - {f"{m.upper()} {p}" for p, o in spec["paths"].items() for m in o}
    )

    if unpublished:
        print("These endpoints exist but have no page in docs/docs.json:")
        for entry in unpublished:
            print(f"  {entry}")
        print('\nAdd each to the matching group\'s "pages" list.')
    if dangling:
        print("\nThese pages name an endpoint the app does not serve:")
        for entry in dangling:
            print(f"  {entry}")
        print("\nRemove them, or regenerate the spec if the route was renamed.")

    if unpublished or dangling:
        return 1
    print(f"docs nav and openapi.json agree ({len(in_spec)} endpoints).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
