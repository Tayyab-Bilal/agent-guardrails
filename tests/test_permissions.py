import pytest
from conftest import DEFAULT, Harness

from agent_guardrails import (
    FilePermissionSource,
    HttpPermissionSource,
    PermissionsMisconfigured,
    PermissionsUnavailable,
    StaticPermissionSource,
)

UNAVAILABLE = "SYSTEM UNAVAILABLE"
MISCONFIGURED = "MISCONFIGURED"


class Flaky:
    def __init__(self, exc):
        self.exc, self.fetches = exc, 0

    async def fetch(self, tenant_id, user_id):
        self.fetches += 1
        raise self.exc


class Http(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


async def test_permission_timeout_is_unavailable_not_disabled(backend, store):
    h = Harness(backend, store, Flaky(TimeoutError("slow")))
    assert UNAVAILABLE in await h.call("create_task", title="x")
    # 5xx is transient too
    assert UNAVAILABLE in await Harness(backend, store, Flaky(Http(503))).call("create_task", title="x")
    assert backend.calls == [] and store.list_for_run("run-1") == []


async def test_malformed_permissions_is_misconfigured(backend, store):
    bad_payloads = (
        {"categories": "oops"}, {"categories": [{"name": "T", "actions": [1]}]}, "nope",
        {"categories": [{"name": "T", "actions": [{"name": "A", "mode": ["Autonomous"]}]}]},
        {"categories": [{"name": "T", "actions": [{"name": {"x": 1}, "mode": "Disabled"}]}]},
        {"categories": [{"name": "T", "actions": [{"name": "A"}]}]},
        {"categories": [{"name": ["T"], "actions": []}]},
    )
    for bad in bad_payloads:
        h = Harness(backend, store, StaticPermissionSource(bad))
        assert MISCONFIGURED in await h.call("create_task", title="x")
    # 4xx is terminal
    assert MISCONFIGURED in await Harness(backend, store, Flaky(Http(403))).call("create_task", title="x")


async def test_file_source_reads_fresh_and_flags_bad_json(tmp_path, backend, store):
    import json

    p = tmp_path / "perms.json"
    p.write_text(json.dumps(DEFAULT))
    h = Harness(backend, store, FilePermissionSource(p))
    assert "task_id" in await h.call("create_task", title="x")
    p.write_text("{not json")
    assert MISCONFIGURED in await h.call("create_task", title="y")


async def test_missing_or_unreadable_permission_file_is_misconfigured(tmp_path, backend, store):
    h = Harness(backend, store, FilePermissionSource(tmp_path / "nope.json"))
    assert MISCONFIGURED in await h.call("create_task", title="x")  # a wrong path is a config bug, not an outage


# Stand-ins named like httpx / requests exceptions (no httpx dependency needed).
class ReadTimeout(Exception):
    pass


class ConnectError(Exception):
    pass


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class HTTPStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Server error '{status_code}' for url 'http://x'")
        self.response = _Resp(status_code)


async def test_http_client_errors_are_classified_like_real_ones(backend, store):
    transient = [ReadTimeout("t"), ConnectError("c"), HTTPStatusError(503),
                 RuntimeError("503 Server Error: Service Unavailable for url: http://x")]
    for exc in transient:
        assert UNAVAILABLE in await Harness(backend, store, Flaky(exc)).call("create_task", title="x")
    assert MISCONFIGURED in await Harness(backend, store, Flaky(HTTPStatusError(404))).call("create_task", title="x")
    assert MISCONFIGURED in await Harness(backend, store, Flaky(RuntimeError("403 Client Error: Forbidden for url"))
                      ).call("create_task", title="x")


async def test_unavailable_is_retried_then_parked_never_reported_as_disabled(backend, store):
    src = Flaky(TimeoutError("slow"))
    out = await Harness(backend, store, src).call("create_task", title="x")
    assert src.fetches == 3  # first try + 2 bounded retries
    assert out.startswith("SYSTEM UNAVAILABLE") and "parked as resumable" in out
    assert "not a policy decision" in out
    assert "disabled" not in out.lower() and "NOT ALLOWED" not in out


async def test_transient_error_that_recovers_runs_the_write(backend, store):
    class RecoversOnThird:
        n = 0

        async def fetch(self, tenant_id, user_id):
            self.n += 1
            if self.n < 3:
                raise ConnectError("blip")
            return DEFAULT

    out = await Harness(backend, store, RecoversOnThird()).call("create_task", title="x")
    assert isinstance(out, dict) and len(backend.calls) == 1


async def test_misconfigured_is_terminal_with_the_real_cause_and_not_retried(backend, store):
    src = Flaky(Http(403))
    out = await Harness(backend, store, src).call("create_task", title="x")
    assert src.fetches == 1  # a terminal cause is never retried
    assert out.startswith("MISCONFIGURED:") and "HTTP 403" in out
    assert "disabled" not in out.lower()
    out = await Harness(backend, store, StaticPermissionSource({"categories": "oops"})).call(
        "create_task", title="x")
    assert out.startswith("MISCONFIGURED:") and "expected shape" in out


async def test_http_source_classifies_errors_through_the_classifier():
    seen = {}

    async def ok(url, headers):
        seen.update(url=url, headers=headers)
        return DEFAULT

    src = HttpPermissionSource("https://perm.test/{tenant_id}/{user_id}", ok, {"x-key": "k"})
    assert await src.fetch("acme", "sam") == DEFAULT
    assert seen == {"url": "https://perm.test/acme/sam", "headers": {"x-key": "k"}}

    def failing(exc):
        async def get_json(url, headers):
            raise exc
        return HttpPermissionSource("https://perm.test", get_json)

    for exc in (ReadTimeout("t"), ConnectError("c"), HTTPStatusError(503)):
        with pytest.raises(PermissionsUnavailable):
            await failing(exc).fetch("a", "b")
    for exc in (HTTPStatusError(404), Http(403), ValueError("bad body")):
        with pytest.raises(PermissionsMisconfigured):
            await failing(exc).fetch("a", "b")


async def test_http_source_works_behind_the_gate(backend, store):
    async def get_json(url, headers):
        return DEFAULT

    h = Harness(backend, store, HttpPermissionSource("https://perm.test/{tenant_id}", get_json))
    assert isinstance(await h.call("create_task", title="x"), dict)
