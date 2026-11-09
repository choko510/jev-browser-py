"""Secret redaction for everything that leaves the page.

Redaction is fail-closed: if a configured secret appears in a string and the
redactor cannot prove it removed every representation, callers get
:class:`RedactionFailure` instead of a partially cleaned string. Nothing here
emits the secret itself, including in error messages.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, quote_plus

from .contracts import RedactionStatus

__all__ = [
    "REDACTED",
    "REPRESENTATION_LIMIT",
    "RedactionFailure",
    "Redactor",
    "build_redactor",
    "detect_secret_markers",
]

REDACTED = "[REDACTED]"
REPRESENTATION_LIMIT = 64
"""Longest secret considered; anything longer is rejected, not truncated."""

_MIN_SECRET_LENGTH = 3
"""Shorter values cannot be redacted safely without destroying all text."""

SECRET_QUERY_KEYS = (
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "code",
    "id_token",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "token",
)

_PII_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,80}\.[A-Za-z]{2,12}")
_PII_CARD = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set-cookie",
    "sessionid",
    "csrf",
    "bearer",
)


class RedactionFailure(RuntimeError):
    """Raised when a configured secret cannot be fully removed."""


class Redactor:
    """Removes known secrets and their encoded representations from text.

    Every configured value is expanded into the representations that can
    realistically appear in page text, HTML, URLs, or JSON: raw, HTML-escaped,
    URL-encoded (both quote styles), JSON-escaped, and base64-ish fragments are
    not attempted because unbounded expansion would make the filter quadratic.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        self._patterns: tuple[re.Pattern[str], ...] = ()
        self._secret_count = 0
        representations: list[str] = []
        for secret in secrets:
            if not secret:
                continue
            if len(secret) > REPRESENTATION_LIMIT:
                raise RedactionFailure("a configured secret exceeds the redaction length limit")
            if len(secret) < _MIN_SECRET_LENGTH:
                raise RedactionFailure("a configured secret is too short to redact safely")
            representations.extend(self._representations(secret))
        # Longest first so a secret that contains another is removed whole
        # before its substring is replaced, keeping output deterministic.
        unique = sorted({value for value in representations if value}, key=len, reverse=True)
        self._secret_count = len(unique)
        self._patterns = tuple(re.compile(re.escape(value)) for value in unique)

    @staticmethod
    def _representations(secret: str) -> list[str]:
        values = {
            secret,
            html.escape(secret),
            html.escape(secret, quote=True),
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
            quote(secret, safe="/:?=&#"),
            secret.replace("\\", "\\\\").replace('"', '\\"'),
            secret.replace("'", "\\'"),
        }
        return [value for value in values if value]

    @property
    def secret_count(self) -> int:
        return self._secret_count

    def has_secrets(self) -> bool:
        return self._secret_count > 0

    def redact(self, text: str) -> str:
        """Replace every known representation; raises if any survives."""
        if not text or not self._patterns:
            return text
        result = text
        for pattern in self._patterns:
            result = pattern.sub(REDACTED, result)
        if self._contains_secret(result):
            raise RedactionFailure("secret survived redaction")
        return result

    def redact_capped(self, text: str, limit: int) -> str:
        """Redact *then* slice, so a cap can never expose a partial secret."""
        if limit <= 0:
            return ""
        return self.redact(text)[:limit]

    def redact_mapping(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Deep redaction of JSON-ish structures; raises on unsupported types."""
        return {str(key): self._redact_value(value) for key, value in payload.items()}

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {str(key): self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._redact_value(item) for item in value]
        if isinstance(value, bool | int | float) or value is None:
            return value
        raise RedactionFailure(f"cannot redact a value of type {type(value).__name__}")

    def _contains_secret(self, text: str) -> bool:
        return any(pattern.search(text) is not None for pattern in self._patterns)

    def contains_secret(self, text: str) -> bool:
        return self._contains_secret(text)

    def status_for(self, *texts: str) -> RedactionStatus:
        """Report whether redaction ran, based only on configured secrets."""
        if not self._patterns:
            return RedactionStatus.NO_SECRETS
        if all(not self._contains_secret(text) for text in texts):
            return RedactionStatus.REDACTED
        return RedactionStatus.FAILED


def build_redactor(secrets: Iterable[str]) -> Redactor:
    """Build a redactor, or an empty one when no secrets are configured."""
    return Redactor(secrets)


def detect_secret_markers(text: str) -> tuple[str, ...]:
    """Return which secret-ish markers appear in text; never returns values.

    Used to decide whether a page is sensitive enough to demand a redactor
    before its content may be shown to a model.
    """
    lowered = text.lower()
    return tuple(marker for marker in _SECRET_MARKERS if marker in lowered)


def redact_query_string(url: str) -> str:
    """Strip secret-looking query parameter values from a URL."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [(key, REDACTED if key.lower() in SECRET_QUERY_KEYS else value) for key, value in pairs]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted, doseq=True), parts.fragment))


def scrub_pii(text: str) -> str:
    """Best-effort PII masking for stored artifacts (not a guarantee)."""
    result = _PII_EMAIL.sub("[EMAIL]", text)
    return _PII_CARD.sub("[CARD]", result)
