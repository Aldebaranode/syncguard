"""Secret extraction and config redaction."""

import pytest

from harness.configs import extract_secret, redact_config, secret_fingerprint

ABC123_SHA256 = "6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090"


def test_extract_quoted_secret_with_comment():
    """Quotes and the trailing comment are not part of the value."""
    assert extract_secret('secret: "abc123" # shared\nnode:\n  id: "v"\n') == "abc123"


def test_extract_plain_secret_with_comment():
    """An unquoted value stops before ' #'."""
    assert extract_secret("secret: plain-value # shared\n") == "plain-value"


def test_missing_secret_is_an_error():
    """A config without a secret line cannot be used for key transfer tests."""
    with pytest.raises(ValueError):
        extract_secret("node:\n  id: v\n")


def test_fingerprint_is_a_sha256_prefix():
    """16 hex characters of sha256(secret)."""
    assert secret_fingerprint("abc123") == ABC123_SHA256[:16]


def test_redaction_replaces_only_the_value():
    """The rest of the file is unchanged and the secret is gone."""
    text = 'secret: "abc123" # shared\nnode:\n  id: "validator-1"\n'
    redacted, fingerprint = redact_config(text)
    assert "abc123" not in redacted
    assert fingerprint == ABC123_SHA256[:16]
    assert redacted == f'secret: "<redacted sha256:{fingerprint}>" # shared\nnode:\n  id: "validator-1"\n'


def test_redaction_without_secret_line():
    """No secret line: text unchanged, empty fingerprint."""
    assert redact_config("node:\n  id: v\n") == ("node:\n  id: v\n", "")
