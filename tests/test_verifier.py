"""PYJEV-T06: a model's `done` alone can never produce success."""

from __future__ import annotations

from conftest import make_snapshot
from jev_browser_py.contracts import PageSnapshot, VerificationResult
from jev_browser_py.verifier import (
    CallableVerifier,
    RuleBasedVerifier,
    VerificationError,
    extract_expectations,
    strip_echoes,
    verify_outcome,
)


def _pair(*, before_text: str, after_text: str, changed: bool = True) -> tuple[PageSnapshot, PageSnapshot]:
    before = make_snapshot(snapshot_id="before", excerpt=before_text, page_fingerprint="aaaa")
    after = make_snapshot(
        snapshot_id="after",
        excerpt=after_text,
        generation=1,
        page_fingerprint="bbbb" if changed else "aaaa",
    )
    return before, after


def test_a_new_sought_phrase_verifies() -> None:
    before, after = _pair(
        before_text="Search page with nothing.",
        after_text="Annual Report 2026 shows revenue of 42 million.",
    )
    result = RuleBasedVerifier().verify_sync(goal='find the phrase "revenue of 42 million"', before=before, after=after)
    assert result.verified is True
    assert result.evidence


def test_a_phrase_that_did_not_move_does_not_verify() -> None:
    text = "Annual Report 2026 shows revenue of 42 million."
    before, after = _pair(before_text=text, after_text=text, changed=False)
    result = RuleBasedVerifier().verify_sync(goal='find the phrase "revenue of 42 million"', before=before, after=after)
    assert result.verified is False
    assert "did not change" in result.detail


def test_a_phrase_that_was_present_and_stayed_present_verifies_when_change_is_not_required() -> None:
    """An explicit confirmation task may legitimately match an unchanged page."""
    text = "Annual Report 2026 shows revenue of 42 million."
    before, after = _pair(before_text=text, after_text=text, changed=False)
    result = RuleBasedVerifier(require_change=False).verify_sync(
        goal='find the phrase "revenue of 42 million"', before=before, after=after
    )
    assert result.verified is True


def test_a_search_echo_of_the_query_never_verifies() -> None:
    """The agent's own typed query printed back is not evidence."""
    before, after = _pair(
        before_text="Search documents",
        after_text='Results for "revenue of 42 million" — 0 documents found.',
    )
    result = RuleBasedVerifier().verify_sync(goal='find the phrase "revenue of 42 million"', before=before, after=after)
    assert result.verified is False


def test_an_unchanged_page_does_not_verify() -> None:
    before, after = _pair(
        before_text="revenue of 42 million appears in the query box",
        after_text="revenue of 42 million appears in the query box",
        changed=False,
    )
    result = RuleBasedVerifier().verify_sync(goal='find "revenue of 42 million"', before=before, after=after)
    assert result.verified is False


def test_a_not_found_page_never_verifies() -> None:
    before, after = _pair(
        before_text="Search page",
        after_text='Results for "annual report": no results found',
    )
    result = RuleBasedVerifier().verify_sync(goal='find "annual report"', before=before, after=after)
    assert result.verified is False


def test_token_overlap_without_explicit_evidence_does_not_verify() -> None:
    before, after = _pair(
        before_text="Home page with links.",
        after_text="Search results: contact support team listed in these matches.",
    )
    result = RuleBasedVerifier().verify_sync(
        goal="go to the contact page and confirm the support team is listed",
        before=before,
        after=after,
    )
    assert result.verified is False
    assert "explicit" in result.detail.lower()


def test_every_explicit_expectation_must_be_present() -> None:
    before, after = _pair(
        before_text="Home page.",
        after_text="Annual Report 2026 is listed, but no revenue value is shown.",
    )
    result = RuleBasedVerifier().verify_sync(
        goal='find "Annual Report 2026" and verify "revenue of 42 million"',
        before=before,
        after=after,
    )
    assert result.verified is False
    assert "revenue of 42 million" in result.detail


def test_an_unrelated_page_does_not_verify() -> None:
    before, after = _pair(
        before_text="Home page.",
        after_text="Completely unrelated content about gardening.",
    )
    result = RuleBasedVerifier().verify_sync(goal="confirm the support team is listed", before=before, after=after)
    assert result.verified is False


def test_an_empty_goal_never_verifies() -> None:
    before, after = _pair(before_text="a", after_text="b")
    assert RuleBasedVerifier().verify_sync(goal="   ", before=before, after=after).verified is False


def test_extract_expectations_finds_quotes_and_targets() -> None:
    assert extract_expectations('find "Annual Report 2026"') == ("Annual Report 2026",)
    assert any(
        "revenue of 42 million" in phrase
        for phrase in extract_expectations("verify that revenue of 42 million is shown")
    )


def test_strip_echoes_removes_the_agents_query() -> None:
    text = 'Results for "annual report" — Annual Report 2026'
    cleaned = strip_echoes(text)
    assert '"annual report"' not in cleaned
    assert "Annual Report 2026" in cleaned


async def test_a_verifier_exception_is_never_success() -> None:
    before, after = _pair(before_text="a", after_text="b")

    async def boom(goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult:
        raise RuntimeError("verifier crashed")

    result = await verify_outcome(CallableVerifier(boom), goal="x", before=before, after=after)
    assert result.verified is False


async def test_a_malformed_verifier_result_is_never_success() -> None:
    before, after = _pair(before_text="a", after_text="b")

    async def bad(goal: str, before: PageSnapshot, after: PageSnapshot) -> object:
        return {"verified": True}

    result = await verify_outcome(CallableVerifier(bad), goal="x", before=before, after=after)
    assert result.verified is False


async def test_verification_error_is_never_success() -> None:
    before, after = _pair(before_text="a", after_text="b")

    async def refuses(goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult:
        raise VerificationError("cannot run")

    result = await verify_outcome(CallableVerifier(refuses), goal="x", before=before, after=after)
    assert result.verified is False


def test_rule_based_verifier_is_async_compatible() -> None:
    import asyncio

    before, after = _pair(
        before_text="Search page",
        after_text="The Annual Report 2026 is ready.",
    )
    result = asyncio.run(RuleBasedVerifier().verify(goal='find "Annual Report 2026"', before=before, after=after))
    assert result.verified is True
