"""Reading and redacting syncguard YAML configs without a YAML library.

Only the top-level `secret:` line matters here: it is read for the HMAC of the
negative key-transfer tests and replaced by a fingerprint before a config is
copied into a session directory.
"""

import hashlib
import re

SECRET_LINE = re.compile(r"^(?P<prefix>\s*secret:\s*)(?P<value>.+?)\s*$", re.MULTILINE)


def _unquote(raw: str) -> str:
    """Strip a trailing YAML comment and surrounding quotes from a scalar."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        closing = value.index(value[0], 1)
        return value[1:closing]
    return value.split(" #", 1)[0].strip()


def extract_secret(text: str) -> str:
    """The shared secret value of a syncguard config."""
    match = SECRET_LINE.search(text)
    if match is None:
        raise ValueError("config has no secret line")
    return _unquote(match["value"])


def secret_fingerprint(secret: str) -> str:
    """First 16 hex characters of sha256(secret): shows two configs share a secret without storing it."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def redact_config(text: str) -> tuple:
    """Replace the secret value by its fingerprint; return the text and the fingerprint ('' without a secret)."""
    match = SECRET_LINE.search(text)
    if match is None:
        return text, ""
    value = match["value"]
    secret = _unquote(value)
    fingerprint = secret_fingerprint(secret)
    comment = value[value.index(" #"):] if " #" in value else ""
    replacement = f'"<redacted sha256:{fingerprint}>"{comment}'
    return text[:match.start("value")] + replacement + text[match.end("value"):], fingerprint
