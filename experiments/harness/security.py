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
from harness.logparse import parse_log_file

PAYLOAD_VALIDATOR_KEY = "SYNCGUARD_VALIDATOR_KEY"
PAYLOAD_FAILOVER_NOTIFY = "SYNCGUARD_FAILOVER_NOTIFY"
PAYLOAD_FAILBACK_NOTIFY = "SYNCGUARD_FAILBACK_NOTIFY"
HEADER_SIGNATURE = "X-Syncguard-Signature"
HEADER_TIMESTAMP = "X-Syncguard-Timestamp"
REPLAY_WINDOW_S = 30
CASE_OFFSET_S = 60
PROBE_BODY = b"syncguard-negative-test"
ROLE_CHANGE_MARKERS = ("failover_notified", "takeover_started", "takeover_complete")


def sign(secret: str, unix_seconds: int, payload: str = PAYLOAD_VALIDATOR_KEY) -> str:
    """HMAC-SHA256 over the payload followed by the timestamp, as hex."""
    message = (payload + str(unix_seconds)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signed(secret: str, unix_seconds: int, payload: str) -> dict:
    """The two authentication headers for one timestamp and payload."""
    return {HEADER_SIGNATURE: sign(secret, unix_seconds, payload), HEADER_TIMESTAMP: str(unix_seconds)}


@dataclass(frozen=True)
class NegativeCase:
    """One request the passive node must refuse, described without secrets."""

    number: int
    name: str
    method: str
    path: str
    request: str
    headers: dict
    expected: int


def build_cases(secret: str, now_s: int) -> list:
    """Cases 1-4 against /validator_key (spec) and 5-7 against /failover_notify (added after 11bbc8f)."""
    stale = now_s - CASE_OFFSET_S
    future = now_s + CASE_OFFSET_S
    return [
        NegativeCase(1, "hmac_salah", "POST", "/validator_key",
                     "POST /validator_key, timestamp sekarang, HMAC dari secret yang salah",
                     signed(secret + "-wrong", now_s, PAYLOAD_VALIDATOR_KEY), 401),
        NegativeCase(2, "timestamp_kedaluwarsa", "POST", "/validator_key",
                     "POST /validator_key, timestamp 60 s lalu, HMAC benar untuk timestamp itu",
                     signed(secret, stale, PAYLOAD_VALIDATOR_KEY), 401),
        NegativeCase(3, "tanpa_header", "POST", "/validator_key",
                     "POST /validator_key tanpa header autentikasi", {}, 401),
        NegativeCase(4, "timestamp_masa_depan", "POST", "/validator_key",
                     "POST /validator_key, timestamp 60 s ke depan, HMAC benar untuk timestamp itu",
                     signed(secret, future, PAYLOAD_VALIDATOR_KEY), 401),
        NegativeCase(5, "notify_tanpa_tanda_tangan", "POST", "/failover_notify",
                     "POST /failover_notify tanpa header autentikasi", {}, 401),
        NegativeCase(6, "notify_payload_kunci", "POST", "/failover_notify",
                     "POST /failover_notify, HMAC benar atas payload kunci validator, bukan payload notifikasi failover",
                     signed(secret, now_s, PAYLOAD_VALIDATOR_KEY), 401),
        NegativeCase(7, "notify_metode_get", "GET", "/failover_notify", "GET /failover_notify", {}, 405),
    ]


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


def run_case(http, target: NegativeTarget, case: NegativeCase, sent_ms: int) -> dict:
    """Send one case, then check the status, the key-side files, and that the passive node kept its role."""
    digests_before = file_digests(key_side_files(target.key_path))
    names_before = directory_names(target.key_path.parent)
    body = PROBE_BODY if case.method == "POST" else None
    response = http.request(case.method, target.base_url + case.path, headers=case.headers, body=body, timeout=5.0)
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
        "status": "LULUS" if passed else "GAGAL",
        "sha256_before": digests_before,
        "sha256_after": digests_after,
    }
