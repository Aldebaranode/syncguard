"""Negative peer-authentication requests against the passive syncguard (III-14).

Signatures follow internal/crypto/auth.go SignWithTimestamp:
hex(HMAC-SHA256(secret, payload + str(unix_seconds))), with the timestamp in
X-Syncguard-Timestamp. Each endpoint has its own payload
(internal/constants/server.go at 11bbc8f) and accepts timestamps within 30 s
either way; the notify endpoints accept POST only. Tables never show the
secret or a signature.
"""

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from harness.constants import BACKUP_NODE
from harness.logparse import ROLE_CHANGE_MARKERS, parse_log_file

PAYLOAD_VALIDATOR_KEY = "SYNCGUARD_VALIDATOR_KEY"
PAYLOAD_FAILOVER_NOTIFY = "SYNCGUARD_FAILOVER_NOTIFY"
PAYLOAD_FAILBACK_NOTIFY = "SYNCGUARD_FAILBACK_NOTIFY"
HEADER_SIGNATURE = "X-Syncguard-Signature"
HEADER_TIMESTAMP = "X-Syncguard-Timestamp"
REPLAY_WINDOW_S = 30
CASE_OFFSET_S = 60
PROBE_BODY = b"syncguard-negative-test"
CONTROL_CASE = "kontrol_hmac_benar"
UNPROVEN = "TIDAK TERBUKTI"


def sign(secret: str, unix_seconds: int, payload: str = PAYLOAD_VALIDATOR_KEY) -> str:
    """HMAC-SHA256 over the payload followed by the timestamp, as hex."""
    message = (payload + str(unix_seconds)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed(secret: str, unix_seconds: int, payload: str) -> dict:
    """The two authentication headers for one timestamp and payload."""
    return {HEADER_SIGNATURE: sign(secret, unix_seconds, payload), HEADER_TIMESTAMP: str(unix_seconds)}


@dataclass(frozen=True)
class NegativeCase:
    """One request the passive node must refuse, described without secrets or signatures.

    secret_kind is "none" (no auth headers), "wrong" (HMAC from another secret)
    or "correct". timestamp_offset_s shifts the signed timestamp from the send
    time. correct_hmac marks cases whose refusal only means something if a
    correctly signed request is shown to get past authentication (the control).
    refusal_marker is the log marker the server writes when it rejects the request.
    """

    number: int
    name: str
    method: str
    path: str
    request: str
    payload: str
    secret_kind: str
    timestamp_offset_s: int
    expected: int
    correct_hmac: bool
    refusal_marker: str | None


def build_cases() -> list:
    """Cases 1-4 against /validator_key (spec), 5-7 against /failover_notify (added after 11bbc8f), 8 the control.

    The control sends a correctly signed, current request; the server passes
    authentication and fails to stage the junk body (crypto.Decrypt fails
    before any write), so it answers 500 with every file unchanged.
    """
    key = PAYLOAD_VALIDATOR_KEY
    rejected_key = "unauthenticated_rejected"
    return [
        NegativeCase(1, "hmac_salah", "POST", "/validator_key",
                     "POST /validator_key, timestamp sekarang, HMAC dari secret yang salah",
                     key, "wrong", 0, 401, False, rejected_key),
        NegativeCase(2, "timestamp_kedaluwarsa", "POST", "/validator_key",
                     "POST /validator_key, timestamp 60 s lalu, HMAC benar untuk timestamp itu",
                     key, "correct", -CASE_OFFSET_S, 401, True, rejected_key),
        NegativeCase(3, "tanpa_header", "POST", "/validator_key",
                     "POST /validator_key tanpa header autentikasi", key, "none", 0, 401, False, rejected_key),
        NegativeCase(4, "timestamp_masa_depan", "POST", "/validator_key",
                     "POST /validator_key, timestamp 60 s ke depan, HMAC benar untuk timestamp itu",
                     key, "correct", CASE_OFFSET_S, 401, True, rejected_key),
        NegativeCase(5, "notify_tanpa_tanda_tangan", "POST", "/failover_notify",
                     "POST /failover_notify tanpa header autentikasi",
                     PAYLOAD_FAILOVER_NOTIFY, "none", 0, 401, False, "notify_rejected"),
        NegativeCase(6, "notify_payload_kunci", "POST", "/failover_notify",
                     "POST /failover_notify, HMAC benar atas payload kunci validator, bukan payload notifikasi failover",
                     key, "correct", 0, 401, True, "notify_rejected"),
        NegativeCase(7, "notify_metode_get", "GET", "/failover_notify", "GET /failover_notify",
                     PAYLOAD_FAILOVER_NOTIFY, "none", 0, 405, False, None),
        NegativeCase(8, CONTROL_CASE, "POST", "/validator_key", "kontrol: HMAC dan timestamp benar",
                     key, "correct", 0, 500, True, None),
    ]


def request_headers(case: NegativeCase, secret: str, now_s: int) -> dict:
    """The authentication headers of a case, signed for the moment of sending."""
    if case.secret_kind == "none":
        return {}
    key = secret + "-wrong" if case.secret_kind == "wrong" else secret
    return signed(key, now_s + case.timestamp_offset_s, case.payload)


@dataclass(frozen=True)
class NegativeTarget:
    """The passive syncguard under test and the files and log that must show no change."""

    base_url: str
    key_path: Path
    passive_log: Path
    utc_offset_seconds: int


def key_side_files(key_path: Path) -> list:
    """The key file and its .real and .staged siblings."""
    return [key_path, Path(f"{key_path}.real"), Path(f"{key_path}.staged")]


def file_digests(paths: list) -> dict:
    """sha256 per file name; None for a missing file."""
    digests = {}
    for path in paths:
        digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return digests


def directory_names(directory: Path) -> list:
    """Sorted names in a directory."""
    return sorted(child.name for child in directory.iterdir())


def role_change_sources(target: NegativeTarget, since_ms: int) -> list:
    """file:line of every takeover line the passive node logged at or after since_ms."""
    if not target.passive_log.exists():
        return []
    events = parse_log_file(target.passive_log, BACKUP_NODE, target.utc_offset_seconds, since_ms=since_ms)
    return [event.source for event in events if event.marker in ROLE_CHANGE_MARKERS]


def run_case(http, target: NegativeTarget, case: NegativeCase, secret: str, clock) -> dict:
    """Sign and send one case now, then check the status, the key-side files, and that the passive node kept its role."""
    digests_before = file_digests(key_side_files(target.key_path))
    names_before = directory_names(target.key_path.parent)
    sent_ms = clock.now_ms()
    headers = request_headers(case, secret, sent_ms // 1000)
    body = PROBE_BODY if case.method == "POST" else None
    response = http.request(case.method, target.base_url + case.path, headers=headers, body=body, timeout=5.0)
    digests_after = file_digests(key_side_files(target.key_path))
    names_after = directory_names(target.key_path.parent)
    files_unchanged = digests_before == digests_after and names_before == names_after
    role_changes = role_change_sources(target, sent_ms)
    passed = response.status == case.expected and files_unchanged and not role_changes
    return {
        "number": case.number,
        "case": case.name,
        "request": case.request,
        "expected": case.expected,
        "actual": response.status,
        "files_unchanged": files_unchanged,
        "role_unchanged": not role_changes,
        "role_change_sources": role_changes,
        "correct_hmac": case.correct_hmac,
        "refusal_logged": refusal_logged(target, case, sent_ms),
        "status": "LULUS" if passed else "GAGAL",
        "sha256_before": digests_before,
        "sha256_after": digests_after,
    }


def refusal_logged(target: NegativeTarget, case: NegativeCase, sent_ms: int) -> bool | None:
    """Whether the passive log shows the case's rejection line after sent_ms; None when the case has none."""
    if case.refusal_marker is None:
        return None
    if not target.passive_log.exists():
        return False
    events = parse_log_file(target.passive_log, BACKUP_NODE, target.utc_offset_seconds, since_ms=sent_ms)
    return any(event.marker == case.refusal_marker for event in events)


def resolve_control(rows: list) -> list:
    """Downgrade every passing correct-HMAC case to unproven unless the control got 500 with files unchanged.

    A 401 for a correctly signed request only shows the HMAC check works if a
    correctly signed request is shown to pass authentication (the control's 500).
    """
    control = next((row for row in rows if row["case"] == CONTROL_CASE), None)
    proven = control is not None and control["actual"] == 500 and control["files_unchanged"]
    if proven:
        return rows
    resolved = []
    for row in rows:
        unproven = row["correct_hmac"] and row["case"] != CONTROL_CASE and row["status"] == "LULUS"
        resolved.append({**row, "status": UNPROVEN} if unproven else row)
    return resolved
