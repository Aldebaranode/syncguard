"""Secret extraction and config redaction."""

import pytest

from harness.configs import extract_secret, redact_config, secret_fingerprint, verify_redacted

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


def test_quoted_value_containing_a_hash_is_one_secret():
    """A ' #' inside quotes belongs to the value; only the text after the closing quote is a comment."""
    text = 'secret: "fake # value" # shared\n'
    assert extract_secret(text) == "fake # value"
    redacted, fingerprint = redact_config(text)
    assert "fake" not in redacted and "value" not in redacted
    assert redacted == f'secret: "<redacted sha256:{fingerprint}>" # shared\n'


def test_secret_repeated_in_a_comment_is_redacted_everywhere():
    """Every occurrence of the value goes, including comments and other keys."""
    text = 'secret: "fake-secret-1"\n# rotate fake-secret-1 monthly\nnode:\n  note: fake-secret-1\n'
    redacted, _ = redact_config(text)
    assert "fake-secret-1" not in redacted


def test_second_secret_key_is_refused():
    """Two secret keys (for example a nested one) make the value ambiguous."""
    with pytest.raises(ValueError, match="more than one"):
        redact_config('secret: "fake-a"\nnode:\n  secret: "fake-b"\n')


def test_block_scalar_secret_is_refused():
    """A multi-line secret cannot be redacted by a line rewrite."""
    for marker in ("|", ">"):
        with pytest.raises(ValueError, match="block scalar"):
            redact_config(f"secret: {marker}\n  fake-line-one\n  fake-line-two\n")


def test_empty_value_followed_by_a_newline_is_refused():
    """The key must not read the next line as its value."""
    with pytest.raises(ValueError, match="empty"):
        redact_config("secret:\nnode:\n  id: fake-node\n")


def test_escaped_quote_is_refused():
    """An escape sequence in a quoted value makes extraction unreliable."""
    with pytest.raises(ValueError, match="escape"):
        redact_config('secret: "fake\\"tail"\n')


def test_verify_redacted_raises_when_the_secret_survives():
    """The final check rejects any copy that still contains the secret."""
    with pytest.raises(ValueError, match="still contains"):
        verify_redacted("leftover fake-secret-1", "fake-secret-1")


def test_unquoted_value_with_a_tab_before_the_comment():
    """A tab then '#' starts the comment, so the value stops there and no repeat survives."""
    text = "secret: fake-abc\t# note fake-abc\n"
    assert extract_secret(text) == "fake-abc"
    redacted, _ = redact_config(text)
    assert "fake-abc" not in redacted


def test_comment_only_and_yaml_indicator_values_are_refused():
    """A comment as the value, an anchor, an alias, or a tag cannot be read as a plain secret."""
    for line in ("secret: # comment", "secret: &a fake-anchor", "secret: *alias", "secret: !tag fake-tagged"):
        with pytest.raises(ValueError):
            redact_config(line + "\n")
