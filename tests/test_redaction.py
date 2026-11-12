"""PYJEV-T10: secrets never reach model input, traces, or errors."""

from __future__ import annotations

import pytest

from jev_browser_py.redaction import (
    REDACTED,
    RedactionFailure,
    Redactor,
    detect_secret_markers,
    redact_query_string,
    redact_url_for_model,
    redact_urls_in_text,
    scrub_pii,
)


def test_raw_secret_is_redacted() -> None:
    redactor = Redactor(["hunter2-correct-horse"])
    assert redactor.redact("token is hunter2-correct-horse here") == f"token is {REDACTED} here"


def test_html_escaped_secret_is_redacted() -> None:
    redactor = Redactor(['pa"ss&word'])
    assert "pa&quot;ss&amp;word" not in redactor.redact("value pa&quot;ss&amp;word leaked")


def test_url_encoded_secret_is_redacted() -> None:
    redactor = Redactor(["p@ss word/1"])
    text = "u?p=p%40ss+word%2F1&q=1"
    assert "p%40ss+word%2F1" not in redactor.redact(text)


def test_redaction_capped_slices_after_redaction() -> None:
    redactor = Redactor(["supersecretvalue"])
    text = "a" * 5 + "supersecretvalue" + "b" * 20
    capped = redactor.redact_capped(text, 10)
    assert "supersecret" not in capped
    assert capped.startswith("aaaaa")


def test_secret_in_deep_mapping_is_redacted() -> None:
    redactor = Redactor(["tok-123456"])
    payload = {
        "url": "https://x.test/?t=tok-123456",
        "nested": [{"body": "header tok-123456"}],
        "count": 3,
        "flag": True,
    }
    cleaned = redactor.redact_mapping(payload)
    assert "tok-123456" not in str(cleaned)
    assert cleaned["count"] == 3
    assert cleaned["flag"] is True


def test_too_short_secret_is_rejected() -> None:
    with pytest.raises(RedactionFailure, match="too short"):
        Redactor(["ab"])


def test_overlong_secret_is_rejected() -> None:
    with pytest.raises(RedactionFailure, match="length limit"):
        Redactor(["x" * 65])


def test_unsupported_value_type_fails_closed() -> None:
    redactor = Redactor(["a-secret-value"])
    with pytest.raises(RedactionFailure, match="cannot redact"):
        redactor.redact_mapping({"obj": object()})


def test_empty_redactor_is_a_noop() -> None:
    redactor = Redactor([])
    assert not redactor.has_secrets()
    assert redactor.redact("anything") == "anything"


def test_secret_count_counts_representations() -> None:
    redactor = Redactor(['a"un&que/secret'])
    assert redactor.secret_count > 1


def test_query_string_secrets_are_stripped() -> None:
    url = "https://x.test/cb?code=abc123&state=ok&access_token=zzz"
    cleaned = redact_query_string(url)
    assert "abc123" not in cleaned
    assert "zzz" not in cleaned
    assert "state=ok" in cleaned


def test_model_url_redaction_masks_unknown_credentials_query_fragment_and_opaque_path() -> None:
    password = "unknown-user-password"
    path_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature-token"
    query_token = "unlisted-query-token-123"
    fragment_token = "fragment-secret-456"
    url = f"https://user:{password}@example.test/reset/{path_token}?unrecognized={query_token}#auth={fragment_token}"

    safe = redact_url_for_model(url)

    assert "example.test" in safe
    for secret in ("user", password, path_token, query_token, fragment_token):
        assert secret not in safe
    assert "[REDACTED]" in safe


def test_absolute_urls_in_model_text_are_sanitized() -> None:
    text = "Continue at https://example.test/callback?opaque=unlisted-token."

    safe = redact_urls_in_text(text)

    assert "unlisted-token" not in safe
    assert safe.endswith(".")


def test_relative_urls_in_model_text_hide_unknown_tokens() -> None:
    path_token = "opaque-relative-path-token-abcdef1234567890"
    query_token = "unlisted-relative-query-token"
    fragment_token = "unlisted-relative-fragment-token"
    text = f"Open /reset/{path_token}?unknown={query_token} or /login#state={fragment_token}"

    safe = redact_urls_in_text(text)

    for secret in (path_token, query_token, fragment_token):
        assert secret not in safe


def test_detect_secret_markers_never_returns_values() -> None:
    markers = detect_secret_markers("Authorization: Bearer abc. cookie=session")
    assert "authorization" in markers
    assert "cookie" in markers
    assert "abc" not in markers


def test_pii_scrubbing_masks_email_and_card_shapes() -> None:
    text = "contact a.person@example.test card 4111 1111 1111 1111"
    scrubbed = scrub_pii(text)
    assert "a.person@example.test" not in scrubbed
    assert "4111" not in scrubbed


def test_redactor_reports_status() -> None:
    assert Redactor([]).status_for("x").value == "no_secrets"
    assert Redactor(["a-secret-value"]).status_for("clean").value == "redacted"
