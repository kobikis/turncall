"""An allowlist pattern must bind the host, not just the string.

`check_url_allowed` matched the whole URL with fnmatch, where `*` spans `/`.
So `https://*.trusted.com/*` — a pattern an operator would reasonably write —
also accepted URLs that merely *mention* the trusted name somewhere after the
authority. The request still leaves for the attacker's host, from inside the
network, which is the whole point of the gate.
"""

import pytest

from turncall.services.url_allowlist import check_url_allowed

TRUSTED = ["https://*.trusted.com/*"]


def _allowed(url: str, patterns: list[str] = TRUSTED) -> bool:
    try:
        check_url_allowed(url, patterns)
        return True
    except ValueError:
        return False


@pytest.mark.unit
class TestTheHostCannotBeForged:
    @pytest.mark.parametrize(
        "url",
        [
            # The trusted name in the path — `*` used to span the `/`.
            "https://evil.com/x.trusted.com/y",
            # ...or in the query.
            "https://evil.com/?u=https://api.trusted.com/",
            # ...or as a userinfo prefix, which `hostname` discards.
            "https://api.trusted.com@evil.com/z",
            # ...or as a fragment.
            "https://evil.com/#api.trusted.com/",
        ],
    )
    def test_a_trusted_name_outside_the_authority_is_refused(self, url: str) -> None:
        assert not _allowed(url), f"{url} slipped past the allowlist"

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.trusted.com/v1/tools",
            "https://mcp.trusted.com/",
            "https://deep.sub.trusted.com/a/b/c?q=1",
        ],
    )
    def test_the_real_thing_still_passes(self, url: str) -> None:
        assert _allowed(url), f"{url} was refused but should be allowed"


@pytest.mark.unit
class TestExistingBehaviourIsPreserved:
    def test_no_patterns_still_allows_everything(self) -> None:
        """Dev mode and the documented BYOM default."""
        assert _allowed("http://anything.internal:9000/x", [])

    @pytest.mark.parametrize("pattern", ["*", "*trusted*"])
    def test_a_pattern_naming_no_host_keeps_matching_the_whole_url(
        self, pattern: str
    ) -> None:
        """These were always blunt instruments. Narrowing them would break
        configs that rely on them, so the host check only applies when the
        pattern actually names a host."""
        assert _allowed("https://anything.trusted.example/x", [pattern])

    def test_a_port_in_the_pattern_does_not_break_the_host_match(self) -> None:
        """`hostname` strips the port — the common local-dev pattern."""
        assert _allowed("http://localhost:8931/mcp", ["http://localhost:*/*"])

    def test_an_unrelated_host_is_still_refused(self) -> None:
        assert not _allowed("https://untrusted.example/v1", TRUSTED)

    def test_the_message_names_the_label_and_the_patterns(self) -> None:
        with pytest.raises(ValueError, match="MCP server url"):
            check_url_allowed("https://evil.com/", TRUSTED, label="MCP server url")

    def test_a_malformed_url_is_refused_rather_than_raising(self) -> None:
        """urlsplit rejects some inputs outright; that must read as 'no host',
        not as a crash inside the gate."""
        assert not _allowed("https://[oops/", TRUSTED)
