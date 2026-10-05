"""Reading and redacting syncguard YAML configs without a YAML library.

Only the top-level `secret:` line matters here: it is read for the HMAC of the
negative key-transfer tests and replaced by a fingerprint before a config is
copied into a session directory. Shapes this reader cannot handle safely
(block scalars, repeated keys, empty values, escape sequences) are refused
instead of guessed at, because a wrong guess would leak the secret.
"""

import hashlib
import re

# [ \t] instead of \s so the key never reads the next line as its value.
SECRET_LINE = re.compile(r"^(?P<indent>[ \t]*)secret:[ \t]*(?P<value>[^\n]*)$", re.MULTILINE)
# A comment starts at whitespace followed by '#'; the whitespace may be a tab.
COMMENT_START = re.compile(r"[ \t]#")


def _parse_value(raw: str) -> tuple:
    """Split a scalar into (secret value, trailing comment text); refuse shapes that cannot be read safely."""
    value = raw.strip()
    if not value:
        raise ValueError("secret value is empty")
    if value[0] in "|>":
        raise ValueError("secret is a block scalar, which this reader does not support")
    if value[0] in "#&*!":
        raise ValueError("secret value starts with a comment, anchor, alias, or tag indicator")
    if value[0] in ('"', "'"):
        quote = value[0]
        closing = value.find(quote, 1)
        if closing == -1:
            raise ValueError("secret value has no closing quote")
        body = value[1:closing]
        if quote == '"' and "\\" in body:
            raise ValueError("secret value contains an escape sequence")
        if quote == "'" and value[closing + 1:closing + 2] == "'":
            raise ValueError("secret value contains an escaped quote")
        if not body:
            raise ValueError("secret value is empty")
        return body, value[closing + 1:]
    comment_start = COMMENT_START.search(value)
    plain = value if comment_start is None else value[:comment_start.start()]
    comment = "" if comment_start is None else value[comment_start.start():]
    plain = plain.strip()
    if not plain:
        raise ValueError("secret value is empty")
    return plain, comment


def _single_secret_line(text: str):
    """The one secret line of a config; None when absent, an error when there is more than one."""
    matches = list(SECRET_LINE.finditer(text))
    if len(matches) > 1:
        raise ValueError("config has more than one secret key")
    return matches[0] if matches else None


def extract_secret(text: str) -> str:
    """The shared secret value of a syncguard config."""
    match = _single_secret_line(text)
    if match is None:
        raise ValueError("config has no secret line")
    return _parse_value(match["value"])[0]


def secret_fingerprint(secret: str) -> str:
    """First 16 hex characters of sha256(secret): shows two configs share a secret without storing it."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def verify_redacted(redacted: str, secret: str) -> None:
    """Raise when a redacted copy still contains the secret; the last gate before anything is written."""
    if secret in redacted:
        raise ValueError("redacted config still contains the secret")


def redact_config(text: str) -> tuple:
    """Replace every occurrence of the secret by its fingerprint; return the text and the fingerprint.

    A config without a secret line comes back unchanged with an empty fingerprint.
    """
    match = _single_secret_line(text)
    if match is None:
        return text, ""
    secret, comment = _parse_value(match["value"])
    fingerprint = secret_fingerprint(secret)
    placeholder = f"<redacted sha256:{fingerprint}>"
    line = f'{match["indent"]}secret: "{placeholder}"{comment}'
    redacted = text[:match.start()] + line + text[match.end():]
    # The secret line is rewritten above; every other occurrence (comments, other keys) goes too.
    redacted = redacted.replace(secret, placeholder)
    verify_redacted(redacted, secret)
    return redacted, fingerprint
