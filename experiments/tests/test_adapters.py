"""HTTP and subprocess adapters."""

import pytest

from fakes import FakeHttp
from harness.adapters import Commands, Http, HttpResponse, RecordedHttp, Unreachable


def test_http_maps_a_refused_connection_to_unreachable():
    """Nothing listens on port 9 locally, so the request fails as Unreachable."""
    with pytest.raises(Unreachable):
        Http().request("GET", "http://127.0.0.1:9/", timeout=1.0)


def test_recorded_http_exact_and_prefix_answers():
    """Exact keys win over prefix keys; the longest prefix wins among prefixes."""
    http = RecordedHttp({
        "http://h/status": {"result": "exact"},
        "http://h/*": {"result": "short"},
        "http://h/commit?height=*": {"result": "long"},
    })
    assert http.get_json("http://h/status") == {"result": "exact"}
    assert http.get_json("http://h/commit?height=5") == {"result": "long"}
    assert http.get_json("http://h/other") == {"result": "short"}


def test_recorded_http_callable_exception_and_status_answers():
    """Callables receive the URL, exceptions are raised, HttpResponse passes through."""
    http = RecordedHttp({
        "http://h/echo*": lambda url: {"url": url},
        "http://h/down": Unreachable("down"),
        "http://h/forbidden": HttpResponse(401, "Unauthorized"),
    })
    assert http.get_json("http://h/echo?x=1") == {"url": "http://h/echo?x=1"}
    with pytest.raises(Unreachable):
        http.get_json("http://h/down")
    assert http.request("POST", "http://h/forbidden").status == 401
    with pytest.raises(Unreachable):
        http.get_json("http://h/forbidden")


def test_recorded_http_unknown_url_is_unreachable():
    """A URL without a recording behaves like an unreachable endpoint."""
    with pytest.raises(Unreachable):
        RecordedHttp({}).get_json("http://h/none")


def test_fake_http_serves_lists_in_order_and_logs_requests():
    """List answers step forward and the last one repeats."""
    http = FakeHttp({"http://h/tip": [{"n": 1}, {"n": 2}]})
    assert [http.get_json("http://h/tip")["n"] for _ in range(3)] == [1, 2, 2]
    assert [request["url"] for request in http.requests] == ["http://h/tip"] * 3


def test_commands_run_captures_output_and_exit_code():
    """A real short command."""
    result = Commands().run(["echo", "hello"])
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.ended_ms >= result.started_ms


def test_commands_timeout_and_missing_program():
    """A timeout returns -1 and a missing program returns 127; neither raises."""
    assert Commands().run(["sleep", "5"], timeout=0.2).exit_code == -1
    assert Commands().run(["no-such-program-for-harness-test"]).exit_code == 127
