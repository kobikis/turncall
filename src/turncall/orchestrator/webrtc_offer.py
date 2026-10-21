"""A browser's WebRTC offer, made safe for pipecat's max-bundle peer (#398).

pipecat 1.13 answers with `bundlePolicy=MAX_BUNDLE`: every m-line shares one
ICE transport, and aiortc fills its remote candidates from each m-line in
turn, so the **last** m-line's win. A browser bundles on the **first** one (the
BUNDLE group's first mid) and closes the others' ports once the answer arrives.
When the browser cannot reach our own address — a container behind Docker
Desktop, a pod — only our checks can connect the call, and they went to a
closed port. Trickle ICE hides it (each PATCHed candidate names its m-line);
an offer that carries its candidates, as the builder's does, did not connect.

So every m-line in the group is given the first one's candidates: whichever
aiortc reads last is then the transport the browser kept.
`tests/unit/test_webrtc_offer.py::test_pipecat_still_needs_it` fails once
pipecat or aiortc reads the first m-line itself.
"""

from typing import Any

_CANDIDATE_LINES = ("a=candidate:", "a=end-of-candidates")


def bundled(signaling: dict[str, Any]) -> dict[str, Any]:
    """A signaling body whose offer is `bundled_on_the_first_mline`."""
    sdp = signaling.get("sdp")
    if not isinstance(sdp, str):
        return signaling
    return {**signaling, "sdp": bundled_on_the_first_mline(sdp)}


def bundled_on_the_first_mline(sdp: str) -> str:
    """The offer with each bundled m-line carrying the group's first one's
    candidates. An offer with no BUNDLE group is returned unchanged."""
    lines = sdp.splitlines(keepends=True)
    group = next((ln for ln in lines if ln.startswith("a=group:BUNDLE")), None)
    if group is None or len(group.split()) < 2:
        return sdp
    mids = group.split()[1:]
    session, sections = _split(lines)
    primary = next((s for s in sections if _mid(s) == mids[0]), None)
    if primary is None:
        return sdp
    candidates = [ln for ln in primary if ln.startswith(_CANDIDATE_LINES)]
    rewritten = [
        _with_candidates(s, candidates) if _mid(s) in mids else s for s in sections
    ]
    return "".join(session + [ln for s in rewritten for ln in s])


def _split(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    session: list[str] = []
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("m="):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            session.append(line)
    return session, sections


def _mid(section: list[str]) -> str | None:
    return next(
        (ln.strip()[len("a=mid:") :] for ln in section if ln.startswith("a=mid:")),
        None,
    )


def _with_candidates(section: list[str], candidates: list[str]) -> list[str]:
    return [ln for ln in section if not ln.startswith(_CANDIDATE_LINES)] + candidates
