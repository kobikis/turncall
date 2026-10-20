"""Plain http:// outbound URLs (#340).

ALLOW_PLAIN_HTTP_URLS decides when it is set; otherwise only development
allows them, so staging and production need https://, or a host that is a
Service inside the cluster. A write is refused with a message naming what
decides it; a delivery to such a URL is only warned about, at most once a
minute per kind and with its host only; startup says which it is, plainly
when development is only TURNCALL_ENV's default.
"""

import pytest
from loguru import logger

from turncall.config.settings import Settings
from turncall.services import outbound_url
from turncall.services.outbound_url import (
    IN_CLUSTER_SUFFIXES,
    check_outbound_url,
    is_https_or_in_cluster,
    log_plain_http_policy,
    plain_http_allowed,
    warn_if_plain_http,
)
from turncall.services.webhook_verification import RefusalLog

pytestmark = pytest.mark.unit

IN_CLUSTER = "http://turncall-agent-9001.turncall.svc.cluster.local:8000/events"


@pytest.fixture
def make(monkeypatch):
    """Settings as the environment would give them, and nothing from `.env`."""

    def _make(env: str | None = None, allow: str | None = None) -> Settings:
        for name, value in (("TURNCALL_ENV", env), ("ALLOW_PLAIN_HTTP_URLS", allow)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return _make


@pytest.fixture
def logs():
    seen: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: seen.append((m.record["level"].name, str(m))), level="INFO"
    )
    yield seen
    logger.remove(sink)


@pytest.fixture
def now(monkeypatch):
    """A fresh delivery log on a clock the test moves."""
    clock = [0.0]
    monkeypatch.setattr(
        outbound_url,
        "plain_http_deliveries",
        RefusalLog(
            interval=60,
            clock=lambda: clock[0],
            note="more plain http deliveries of this kind since the last",
        ),
    )
    return clock


class TestPlainHttpAllowed:
    @pytest.mark.parametrize(
        ("env", "allow", "allowed"),
        [
            (None, None, True),  # TURNCALL_ENV's default is development
            ("development", None, True),
            ("staging", None, False),
            ("production", None, False),
            ("development", "", True),  # blank is unset
            ("production", "", False),
            ("development", "true", True),
            ("staging", "true", True),
            ("production", "true", True),
            (None, "false", False),
            ("development", "false", False),
            ("production", "false", False),
        ],
    )
    def test_the_setting_decides_else_development_only(
        self, make, env, allow, allowed
    ) -> None:
        assert plain_http_allowed(make(env, allow)) is allowed

    def test_settings_default_to_the_process_ones(self, make, monkeypatch) -> None:
        production = make("production")
        monkeypatch.setattr(outbound_url, "get_settings", lambda: production)

        assert plain_http_allowed() is False
        with pytest.raises(ValueError):
            check_outbound_url("http://crm.example.com/hook", label="url")


class TestIsHttpsOrInCluster:
    def test_the_suffixes_are_a_kubernetes_service(self) -> None:
        assert IN_CLUSTER_SUFFIXES == (".svc", ".svc.cluster.local")

    @pytest.mark.parametrize(
        ("url", "allowed"),
        [
            ("https://crm.example.com/hook", True),
            ("HTTPS://crm.example.com/hook", True),
            (" https://crm.example.com/hook ", True),
            (IN_CLUSTER, True),
            ("http://turncall-agent-9001.turncall.svc:8000/events", True),
            ("http://crm.example.com/hook", False),
            # neither loopback nor a private range is exempt
            ("http://127.0.0.1:9001/hook", False),
            ("http://localhost/hook", False),
            ("http://10.0.0.5/hook", False),
            ("http://169.254.169.254/latest/meta-data", False),
            # the suffix must be the host's, not smuggled into the URL elsewhere
            ("http://evil.com/x.svc.cluster.local", False),
            ("http://evil.com/?h=x.svc", False),
            ("http://evil.com#.svc", False),
            ("http://x.svc.cluster.local@evil.com/", False),
            ("http://evil.svc.cluster.local.evil.com/", False),
            ("http://svc/", False),
            ("ftp://a.svc.cluster.local/", False),
            ("a.svc.cluster.local", False),
            ("http://[::1/hook", False),  # unparseable
            ("", False),
        ],
    )
    def test_https_or_a_service_host(self, url, allowed) -> None:
        assert is_https_or_in_cluster(url) is allowed


class TestCheckOutboundUrl:
    def test_returns_the_url_stripped(self, make) -> None:
        assert (
            check_outbound_url(
                "  https://crm.example.com/hook\n", label="url", settings=make()
            )
            == "https://crm.example.com/hook"
        )

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_plain_http_is_refused_outside_development(self, make, env) -> None:
        with pytest.raises(ValueError) as raised:
            check_outbound_url(
                "http://crm.example.com/hook", label="webhook url", settings=make(env)
            )

        message = str(raised.value)
        assert message.startswith("webhook url must use https://")
        assert f"TURNCALL_ENV={env}" in message
        assert "ALLOW_PLAIN_HTTP_URLS=true" in message

    def test_the_opt_out_refuses_it_in_development_too(self, make) -> None:
        with pytest.raises(ValueError) as raised:
            check_outbound_url(
                "http://localhost:9001/hook",
                label="server_url",
                settings=make("development", "false"),
            )

        message = str(raised.value)
        assert message.startswith("server_url must use https://")
        assert "ALLOW_PLAIN_HTTP_URLS=false" in message
        assert "TURNCALL_ENV" in message

    @pytest.mark.parametrize(
        ("env", "allow"),
        [(None, None), ("development", None), ("production", "true")],
    )
    def test_plain_http_is_allowed_where_the_rule_says(self, make, env, allow) -> None:
        url = "http://localhost:9001/hook"
        assert check_outbound_url(url, label="url", settings=make(env, allow)) == url

    @pytest.mark.parametrize("allow", [None, "false"])
    def test_a_service_host_is_allowed_everywhere(self, make, allow) -> None:
        settings = make("production", allow)
        assert check_outbound_url(IN_CLUSTER, label="url", settings=settings) == (
            IN_CLUSTER
        )

    def test_https_is_allowed_everywhere(self, make) -> None:
        url = "https://crm.example.com/hook"
        settings = make("production", "false")
        assert check_outbound_url(url, label="url", settings=settings) == url

    @pytest.mark.parametrize(
        "url", ["ftp://crm.example.com/hook", "ws://crm.example.com"]
    )
    def test_another_scheme_is_refused_even_where_http_is_allowed(
        self, make, url
    ) -> None:
        scheme = url.split(":")[0]
        with pytest.raises(ValueError) as raised:
            check_outbound_url(url, label="url", settings=make("development", "true"))

        # It names what this deployment takes, http:// included.
        assert str(raised.value) == (
            f"url must be an http:// or https:// URL, not {scheme}://"
        )

    def test_another_scheme_names_only_https_where_http_is_refused(self, make) -> None:
        with pytest.raises(ValueError) as raised:
            check_outbound_url(
                "ftp://x.example/", label="url", settings=make("staging")
            )

        assert str(raised.value) == "url must be an https:// URL, not ftp://"

    @pytest.mark.parametrize("url", ["localhost:8080/hook", "crm.example.com/hook"])
    @pytest.mark.parametrize(
        ("env", "start"),
        [("development", "https:// or http://"), ("production", "https://")],
    )
    def test_a_url_without_a_scheme_is_told_to_add_one(
        self, make, url, env, start
    ) -> None:
        with pytest.raises(ValueError) as raised:
            check_outbound_url(url, label="server_url", settings=make(env))

        message = str(raised.value)
        assert message.endswith(f"it has no scheme, so start it with {start}")
        # `localhost:` is not taken for a scheme.
        assert "localhost://" not in message

    @pytest.mark.parametrize(
        "url", ["https://", "https:///hook", "https://:443/hook", "http://"]
    )
    @pytest.mark.parametrize("allow", [None, "true"])
    def test_a_url_without_a_host_is_refused(self, make, url, allow) -> None:
        with pytest.raises(ValueError, match=r"^url must name a host"):
            check_outbound_url(url, label="url", settings=make("development", allow))

    def test_an_unparseable_url_is_refused(self, make) -> None:
        with pytest.raises(ValueError, match=r"^url is not a URL"):
            check_outbound_url("https://[::1/hook", label="url", settings=make())

    def test_a_smuggled_service_suffix_is_refused(self, make) -> None:
        with pytest.raises(ValueError):
            check_outbound_url(
                "http://x.svc.cluster.local@evil.com/",
                label="url",
                settings=make("production"),
            )


class TestWarnIfPlainHttp:
    def test_warns_once_per_interval_per_kind_and_says_what_it_left_out(
        self, make, now, logs
    ) -> None:
        settings = make("production")
        url = "http://crm.example.com/hook?token=s3cret"

        assert warn_if_plain_http("webhook", url, settings=settings, project_id="p1")
        assert not warn_if_plain_http("webhook", url, settings=settings)
        assert not warn_if_plain_http("webhook", url, settings=settings)
        assert warn_if_plain_http("tool", url, settings=settings)
        now[0] = 61.0
        assert warn_if_plain_http("webhook", url, settings=settings)

        lines = [line for level, line in logs if level == "WARNING"]
        assert len(lines) == 3
        assert all("plain_http_delivery" in line for line in lines)
        assert "a webhook delivery to plain http:// host crm.example.com" in lines[0]
        assert "a tool delivery" in lines[1]
        assert (
            "(2 more plain http deliveries of this kind since the last)" in (lines[2])
        )
        # the host only: a path or query can carry a token
        assert not any("s3cret" in line or "/hook" in line for line in lines)

    def test_fields_reach_the_record_bounded(self, make, now) -> None:
        records: list[dict] = []
        sink = logger.add(lambda m: records.append(m.record["extra"]), level="INFO")
        try:
            warn_if_plain_http(
                "call_init",
                "http://crm.example.com/x",
                settings=make("staging"),
                project_id="p\n" + "1" * 200,
            )
        finally:
            logger.remove(sink)

        (extra,) = records
        assert extra["route"] == "call_init"
        assert extra["host"] == "crm.example.com"
        assert "\n" not in extra["project_id"]
        assert len(extra["project_id"]) <= 67

    @pytest.mark.parametrize(
        "url", ["https://crm.example.com/hook", IN_CLUSTER, "ftp://crm.example.com"]
    )
    def test_silent_for_what_the_gate_lets_through(self, make, now, logs, url) -> None:
        assert not warn_if_plain_http("mcp", url, settings=make("production"))
        assert logs == []

    @pytest.mark.parametrize(("env", "allow"), [(None, None), ("production", "true")])
    def test_silent_where_plain_http_is_allowed(self, make, now, logs, env, allow):
        settings = make(env, allow)
        assert not warn_if_plain_http("webhook", "http://x.example", settings=settings)
        assert logs == []

    def test_an_unknown_kind_is_logged_never_raised(self, make, now, logs) -> None:
        assert not warn_if_plain_http(
            "sms", "http://x.example", settings=make("production")
        )
        assert [line for _, line in logs if "plain_http_warning_unknown_kind" in line]
        assert not any("plain_http_delivery" in line for _, line in logs)

    def test_nothing_it_meets_is_raised_into_the_delivery(
        self, make, logs, monkeypatch
    ) -> None:
        class Broken:
            def warn(self, *_: object, **__: object) -> bool:
                raise RuntimeError("the log is broken")

        monkeypatch.setattr(outbound_url, "plain_http_deliveries", Broken())

        assert not warn_if_plain_http(
            "tool", "http://x.example", settings=make("production")
        )
        assert [line for _, line in logs if "plain_http_warning_failed" in line]

    def test_each_server_event_kind_is_counted_apart(self, make, now) -> None:
        settings = make("production")
        url = "http://crm.example.com/events"

        assert warn_if_plain_http("call_init", url, settings=settings)
        assert warn_if_plain_http("server_event", url, settings=settings)


class TestStartupLine:
    def test_allowed_only_because_turncall_env_defaults_is_said_plainly(
        self, make, logs
    ) -> None:
        message = log_plain_http_policy(make())

        assert [lvl for lvl, line in logs if message in line] == ["WARNING"]
        assert "are allowed, because TURNCALL_ENV is not set" in message
        assert "set TURNCALL_ENV=production (or staging)" in message

    @pytest.mark.parametrize(
        ("env", "allow", "level", "said"),
        [
            ("development", None, "INFO", "are allowed (TURNCALL_ENV=development)"),
            ("staging", None, "INFO", "are refused (TURNCALL_ENV=staging)"),
            ("production", None, "INFO", "are refused (TURNCALL_ENV=production)"),
            (
                "production",
                "false",
                "INFO",
                "are refused (ALLOW_PLAIN_HTTP_URLS=false)",
            ),
            (None, "false", "INFO", "are refused (ALLOW_PLAIN_HTTP_URLS=false)"),
            (None, "true", "INFO", "are allowed (ALLOW_PLAIN_HTTP_URLS=true)"),
            (
                "production",
                "true",
                "WARNING",
                "are allowed (ALLOW_PLAIN_HTTP_URLS=true) on a production deployment",
            ),
        ],
    )
    def test_says_which_and_why(self, make, logs, env, allow, level, said) -> None:
        message = log_plain_http_policy(make(env, allow))

        assert message.startswith("plain_http_urls | ")
        assert said in message
        assert [lvl for lvl, line in logs if message in line] == [level]
