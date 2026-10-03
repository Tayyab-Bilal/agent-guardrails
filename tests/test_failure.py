import pytest

from agent_guardrails.failure import Failure, classify, run_with_retry


def test_classify_1404_is_not_404():
    assert classify("task 1404 timed out") is Failure.TRANSIENT  # a 404 match would be terminal
    assert classify("HTTP 404 for task") is Failure.TERMINAL
    assert classify("HTTP 5040") is Failure.TERMINAL  # unknown -> terminal, not a 504


def test_terminal_markers_win_over_transient():
    assert classify("403 access denied after timeout") is Failure.TERMINAL
    assert classify(TimeoutError("x")) is Failure.TRANSIENT
    assert classify("HTTP 503") is Failure.TRANSIENT and classify("HTTP 429 slow down") is Failure.TRANSIENT


async def test_transient_retried_terminal_not():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("HTTP 503 upstream")
        return "ok"

    assert await run_with_retry(flaky, backoff=0) == "ok" and calls["n"] == 3

    calls["n"] = 0

    async def bad():
        calls["n"] += 1
        raise RuntimeError("HTTP 404 not found")

    with pytest.raises(RuntimeError):
        await run_with_retry(bad, backoff=0)
    assert calls["n"] == 1

    async def always():
        calls["n"] += 1
        raise TimeoutError

    calls["n"] = 0
    with pytest.raises(TimeoutError):
        await run_with_retry(always, max_retries=2, backoff=0)
    assert calls["n"] == 3


def test_numbers_outside_a_status_context_are_not_status_codes():
    assert classify("created 500 tasks") is Failure.TERMINAL  # not an outage
    assert classify("HTTP 500 internal error") is Failure.TRANSIENT
    assert classify("503 Service Unavailable") is Failure.TRANSIENT

    class E(Exception):
        status_code = 503

    assert classify(E("boom")) is Failure.TRANSIENT


def test_http_client_exception_shapes():
    class ReadTimeout(Exception):
        pass

    class HTTPStatusError(Exception):
        def __init__(self, code):
            super().__init__("boom")
            self.response = type("R", (), {"status_code": code})()

    assert classify(ReadTimeout("x")) is Failure.TRANSIENT
    assert classify(HTTPStatusError(503)) is Failure.TRANSIENT
    assert classify(HTTPStatusError(404)) is Failure.TERMINAL
    assert classify("503 Server Error: Service Unavailable for url") is Failure.TRANSIENT
    assert classify("404 Client Error: Not Found for url") is Failure.TERMINAL
