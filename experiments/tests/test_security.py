"""Negative peer-authentication requests (III-14)."""

from fakes import FakeHttp
from harness.adapters import HttpResponse
from harness.security import NegativeTarget, build_cases, run_case, sign

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


def test_the_seven_cases():
    """Four key-transfer cases and three notify cases, in table order."""
    cases = build_cases(SECRET, NOW_S)
    assert [(case.number, case.name, case.method, case.path, case.expected) for case in cases] == [
        (1, "hmac_salah", "POST", "/validator_key", 401),
        (2, "timestamp_kedaluwarsa", "POST", "/validator_key", 401),
        (3, "tanpa_header", "POST", "/validator_key", 401),
        (4, "timestamp_masa_depan", "POST", "/validator_key", 401),
        (5, "notify_tanpa_tanda_tangan", "POST", "/failover_notify", 401),
        (6, "notify_payload_kunci", "POST", "/failover_notify", 401),
        (7, "notify_metode_get", "GET", "/failover_notify", 405),
    ]
    by_name = {case.name: case for case in cases}
    assert by_name["hmac_salah"].headers["X-Syncguard-Timestamp"] == "1791172800"
    assert by_name["hmac_salah"].headers["X-Syncguard-Signature"] != KEY_PAYLOAD_SIGNATURE
    assert by_name["timestamp_kedaluwarsa"].headers == {
        "X-Syncguard-Signature": "a4bce65118c70584349823ce61ed657356b9de87ebbed69533dfb64b01eb08da",
        "X-Syncguard-Timestamp": "1791172740",
    }
    assert by_name["tanpa_header"].headers == {}
    assert by_name["timestamp_masa_depan"].headers["X-Syncguard-Timestamp"] == "1791172860"
    assert by_name["notify_payload_kunci"].headers == {
        "X-Syncguard-Signature": KEY_PAYLOAD_SIGNATURE, "X-Syncguard-Timestamp": "1791172800"}
    assert by_name["notify_tanpa_tanda_tangan"].headers == {}
    assert all(SECRET not in case.request for case in cases)


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
    row = run_case(http, target, build_cases(SECRET, NOW_S)[2], SENT_MS)
    assert (row["case"], row["expected"], row["actual"], row["files_unchanged"], row["role_unchanged"], row["status"]) == (
        "tanpa_header", 401, 401, True, True, "LULUS")
    assert http.requests[0]["method"] == "POST"
    assert http.requests[0]["headers"] == {}
    assert row["sha256_before"] == row["sha256_after"]


def test_get_on_notify_expects_405_and_sends_no_body(tmp_path):
    """Case 7 is a bodiless GET."""
    target = make_target(tmp_path)
    http = FakeHttp({BASE + "/failover_notify": HttpResponse(405, "Method not allowed\n")})
    row = run_case(http, target, build_cases(SECRET, NOW_S)[6], SENT_MS)
    assert (row["actual"], row["status"]) == (405, "LULUS")
    assert (http.requests[0]["method"], http.requests[0]["body"]) == ("GET", None)


def test_a_new_file_fails_the_case(tmp_path):
    """A file appearing during the request fails the case even with 401."""
    target = make_target(tmp_path)

    def answer_and_write(url):
        """Simulate a server that wrote a file before answering."""
        target.key_path.with_name("unexpected.json").write_text("{}")
        return HttpResponse(401, "Unauthorized\n")

    row = run_case(FakeHttp({BASE + "/validator_key": answer_and_write}), target, build_cases(SECRET, NOW_S)[0], SENT_MS)
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
                   build_cases(SECRET, NOW_S)[5], SENT_MS)
    assert (row["role_unchanged"], row["role_change_sources"], row["status"]) == (
        False, ["syncguard-passive.log:1"], "GAGAL")


def test_a_wrong_status_fails_the_case(tmp_path):
    """Anything other than the expected status fails."""
    target = make_target(tmp_path)
    http = FakeHttp({BASE + "/validator_key": HttpResponse(500, "Failed to stage key\n")})
    row = run_case(http, target, build_cases(SECRET, NOW_S)[1], SENT_MS)
    assert (row["actual"], row["status"]) == (500, "GAGAL")
