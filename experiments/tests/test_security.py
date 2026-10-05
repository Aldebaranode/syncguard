"""Negative peer-authentication requests (III-14)."""

from fakes import FakeClock, FakeHttp
from harness.adapters import HttpResponse
from harness.logparse import ROLE_CHANGE_MARKERS as LOGPARSE_ROLE_CHANGE_MARKERS
from harness.security import ROLE_CHANGE_MARKERS, NegativeTarget, build_cases, request_headers, resolve_control, run_case, sign

SECRET = "test-secret"
NOW_S = 1791172800
SENT_MS = 1791169200000  # 2026-10-05 10:00:00.000 WIB
BASE = "http://localhost:8081"
KEY_PAYLOAD_SIGNATURE = "b7f5cc9d09b2aa1074e9f71ca5b3972d637d8c52422e0348d1b5a46d795306bc"


def test_sign_matches_an_independent_hmac():
    """Vectors from: printf '<payload><ts>' | openssl dgst -sha256 -hmac 'test-secret'."""
    assert sign(SECRET, 1791172800) == KEY_PAYLOAD_SIGNATURE
    assert sign(SECRET, 1791172740) == "a4bce65118c70584349823ce61ed657356b9de87ebbed69533dfb64b01eb08da"
    assert sign(SECRET, 1791172800, "SYNCGUARD_FAILOVER_NOTIFY") == (
        "624480270f118d5116264b6e9a9ca566af6962ce04544966688a384c2203cea5")


def test_the_eight_cases():
    """Four key-transfer cases, three notify cases, and the correct-HMAC control, in table order."""
    cases = build_cases()
    assert [(case.number, case.name, case.method, case.path, case.expected) for case in cases] == [
        (1, "hmac_salah", "POST", "/validator_key", 401),
        (2, "timestamp_kedaluwarsa", "POST", "/validator_key", 401),
        (3, "tanpa_header", "POST", "/validator_key", 401),
        (4, "timestamp_masa_depan", "POST", "/validator_key", 401),
        (5, "notify_tanpa_tanda_tangan", "POST", "/failover_notify", 401),
        (6, "notify_payload_kunci", "POST", "/failover_notify", 401),
        (7, "notify_metode_get", "GET", "/failover_notify", 405),
        (8, "kontrol_hmac_benar", "POST", "/validator_key", 500),
    ]
    by_name = {case.name: case for case in cases}
    assert by_name["kontrol_hmac_benar"].request == "kontrol: HMAC dan timestamp benar"
    assert [case.name for case in cases if case.correct_hmac] == [
        "timestamp_kedaluwarsa", "timestamp_masa_depan", "notify_payload_kunci", "kontrol_hmac_benar"]
    assert all(SECRET not in case.request for case in cases)


def test_headers_are_signed_from_the_send_time():
    """The same case signed at two times gets two different timestamps and signatures."""
    cases = {case.name: case for case in build_cases()}
    first = request_headers(cases["hmac_salah"], SECRET, NOW_S)
    assert first["X-Syncguard-Timestamp"] == "1791172800"
    assert first["X-Syncguard-Signature"] != KEY_PAYLOAD_SIGNATURE
    assert request_headers(cases["timestamp_kedaluwarsa"], SECRET, NOW_S) == {
        "X-Syncguard-Signature": "a4bce65118c70584349823ce61ed657356b9de87ebbed69533dfb64b01eb08da",
        "X-Syncguard-Timestamp": "1791172740",
    }
    assert request_headers(cases["tanpa_header"], SECRET, NOW_S) == {}
    assert request_headers(cases["notify_tanpa_tanda_tangan"], SECRET, NOW_S) == {}
    assert request_headers(cases["timestamp_masa_depan"], SECRET, NOW_S)["X-Syncguard-Timestamp"] == "1791172860"
    assert request_headers(cases["notify_payload_kunci"], SECRET, NOW_S) == {
        "X-Syncguard-Signature": KEY_PAYLOAD_SIGNATURE, "X-Syncguard-Timestamp": "1791172800"}
    assert request_headers(cases["kontrol_hmac_benar"], SECRET, NOW_S + 5)["X-Syncguard-Timestamp"] == "1791172805"


def test_run_case_signs_at_send_time_not_up_front(tmp_path):
    """A clock that moved on between building the cases and sending one is reflected in the request."""
    target = make_target(tmp_path)
    cases = build_cases()
    clock = FakeClock(SENT_MS)
    clock.sleep(1000)
    http = FakeHttp({BASE + "/validator_key": HttpResponse(500, "Failed to stage key\n")})
    row = run_case(http, target, {case.name: case for case in cases}["kontrol_hmac_benar"], SECRET, clock)
    sent_s = clock.now_ms() // 1000
    assert http.requests[0]["headers"]["X-Syncguard-Timestamp"] == str(sent_s)
    assert http.requests[0]["headers"]["X-Syncguard-Signature"] == sign(SECRET, sent_s)
    assert (row["actual"], row["status"], row["correct_hmac"]) == (500, "LULUS", True)


def test_control_failure_makes_correct_hmac_cases_unproven():
    """If the control does not get 500, a 401 on a correctly signed request proves nothing about the HMAC."""
    def row(case, actual, status):
        """A minimal result row."""
        return {"case": case.name, "actual": actual, "status": status, "files_unchanged": True,
                "correct_hmac": case.correct_hmac}

    cases = build_cases()
    broken = [row(case, 401, "LULUS") for case in cases]
    broken[-1] = row(cases[-1], 401, "GAGAL")
    resolved = {item["case"]: item["status"] for item in resolve_control(broken)}
    assert resolved["hmac_salah"] == "LULUS"
    assert resolved["tanpa_header"] == "LULUS"
    assert resolved["timestamp_kedaluwarsa"] == "TIDAK TERBUKTI"
    assert resolved["timestamp_masa_depan"] == "TIDAK TERBUKTI"
    assert resolved["notify_payload_kunci"] == "TIDAK TERBUKTI"
    assert resolved["kontrol_hmac_benar"] == "GAGAL"
    healthy = [row(case, case.expected, "LULUS") for case in cases]
    assert [item["status"] for item in resolve_control(healthy)] == ["LULUS"] * 8
    assert resolve_control(healthy[:-1])[1]["status"] == "TIDAK TERBUKTI"


def test_role_change_markers_are_one_shared_definition():
    """preconditions and security use the logparse tuple, which includes key_received."""
    assert ROLE_CHANGE_MARKERS is LOGPARSE_ROLE_CHANGE_MARKERS
    assert "key_received" in ROLE_CHANGE_MARKERS


def make_target(tmp_path) -> NegativeTarget:
    """A key directory with the key, .real and .staged files, and an empty passive log."""
    key = tmp_path / "config" / "priv_validator_key.json"
    key.parent.mkdir(parents=True)
    key.write_text('{"address": "E6FA"}')
    key.with_name("priv_validator_key.json.real").write_text('{"address": "48DC"}')
    key.with_name("priv_validator_key.json.staged").write_text('{"address": "48DC"}')
    log = tmp_path / "syncguard-passive.log"
    log.write_text("")
    return NegativeTarget(BASE, key, log, 25200)


def test_refused_request_with_nothing_changed_passes(tmp_path):
    """Expected status, byte-identical files, no takeover line: LULUS."""
    target = make_target(tmp_path)
    http = FakeHttp({BASE + "/validator_key": HttpResponse(401, "Unauthorized\n")})
    row = run_case(http, target, build_cases()[2], SECRET, FakeClock(SENT_MS))
    assert (row["case"], row["expected"], row["actual"], row["files_unchanged"], row["role_unchanged"], row["status"]) == (
        "tanpa_header", 401, 401, True, True, "LULUS")
    assert http.requests[0]["method"] == "POST"
    assert http.requests[0]["headers"] == {}
    assert row["sha256_before"] == row["sha256_after"]


def test_get_on_notify_expects_405_and_sends_no_body(tmp_path):
    """Case 7 is a bodiless GET."""
    target = make_target(tmp_path)
    http = FakeHttp({BASE + "/failover_notify": HttpResponse(405, "Method not allowed\n")})
    row = run_case(http, target, build_cases()[6], SECRET, FakeClock(SENT_MS))
    assert (row["actual"], row["status"]) == (405, "LULUS")
    assert (http.requests[0]["method"], http.requests[0]["body"]) == ("GET", None)


def test_a_new_file_fails_the_case(tmp_path):
    """A file appearing during the request fails the case even with 401."""
    target = make_target(tmp_path)

    def answer_and_write(url):
        """Simulate a server that wrote a file before answering."""
        target.key_path.with_name("unexpected.json").write_text("{}")
        return HttpResponse(401, "Unauthorized\n")

    row = run_case(FakeHttp({BASE + "/validator_key": answer_and_write}), target, build_cases()[0], SECRET,
                   FakeClock(SENT_MS))
    assert (row["files_unchanged"], row["status"]) == (False, "GAGAL")


def test_a_takeover_line_fails_the_case(tmp_path):
    """The passive node logging a takeover after the request means it changed role."""
    target = make_target(tmp_path)

    def answer_and_take_over(url):
        """Simulate a server that took over before answering."""
        target.passive_log.write_text(
            'time="2026-10-05 10:00:00.500" level=info msg="Successfully took over as active validator" '
            'module=server node=validator-2\n')
        return HttpResponse(401, "Unauthorized\n")

    row = run_case(FakeHttp({BASE + "/failover_notify": answer_and_take_over}), target,
                   build_cases()[5], SECRET, FakeClock(SENT_MS))
    assert (row["role_unchanged"], row["role_change_sources"], row["status"]) == (
        False, ["syncguard-passive.log:1"], "GAGAL")


def test_a_wrong_status_fails_the_case(tmp_path):
    """Anything other than the expected status fails."""
    target = make_target(tmp_path)
    http = FakeHttp({BASE + "/validator_key": HttpResponse(500, "Failed to stage key\n")})
    row = run_case(http, target, build_cases()[1], SECRET, FakeClock(SENT_MS))
    assert (row["actual"], row["status"]) == (500, "GAGAL")


def test_refusal_log_line_is_recorded(tmp_path):
    """The case row says whether the expected rejection line (unauthenticated_rejected or notify_rejected) appeared."""
    target = make_target(tmp_path)

    def answer_and_log(url):
        """Simulate a server that logged its rejection before answering."""
        target.passive_log.write_text(
            'time="2026-10-05 10:00:00.500" level=warning msg="Rejected unauthenticated validator key request from 127.0.0.1:5"\n')
        return HttpResponse(401, "Unauthorized\n")

    logged = run_case(FakeHttp({BASE + "/validator_key": answer_and_log}), target, build_cases()[2], SECRET, FakeClock(SENT_MS))
    silent = run_case(FakeHttp({BASE + "/validator_key": HttpResponse(401, "")}), make_target(tmp_path / "b"),
                      build_cases()[2], SECRET, FakeClock(SENT_MS))
    assert logged["refusal_logged"] is True and silent["refusal_logged"] is False
    control = run_case(FakeHttp({BASE + "/validator_key": HttpResponse(500, "")}), make_target(tmp_path / "c"),
                       build_cases()[7], SECRET, FakeClock(SENT_MS))
    assert control["refusal_logged"] is None
