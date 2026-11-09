"""Independent outcome verification.

A model's `goal_done` is a stop *proposal*; it is never the success authority.
The verifier is a separate object with its own evidence rules, and the run
loop refuses to report success unless it returns `verified=True`.

Two implementations ship:

* :class:`RuleBasedVerifier` — offline, deterministic rules over the goal text
  and the observed page. Used by every test and by the local E2E fixtures.
* :class:`CallableVerifier` — wraps a host-provided async check (for example a
  future Harness Sentinel adapter).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .contracts import EvidenceRef, PageSnapshot, VerificationResult

__all__ = [
    "CallableVerifier",
    "RuleBasedVerifier",
    "VerificationError",
    "Verifier",
    "extract_expectations",
    "verify_outcome",
]

_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]{2,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "then",
        "than",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "not",
        "but",
        "its",
        "our",
        "your",
        "their",
        "page",
        "site",
        "website",
        "click",
        "open",
        "go",
        "goto",
        "confirm",
        "verify",
        "find",
        "show",
        "locate",
        "extract",
        "check",
        "listed",
        "shown",
        "appears",
        "reports",
        "report",
        "result",
        "results",
    }
)
_NEGATIONS = ("not found", "no results", "0 results", "page not found", "error")


def _tokens(text: str) -> set[str]:
    """Content-token overlap for diagnostics only; never a success criterion."""
    return {token for token in _TOKEN.findall(text.lower()) if token not in _STOPWORDS}


_ECHO_PATTERNS = (
    re.compile(r'results?\s+for\s+"[^"]{0,200}"', re.IGNORECASE),
    re.compile(r"results?\s+for\s+“[^”]{0,200}”", re.IGNORECASE),
    re.compile(r"results?\s+for\s+[^\n<]{1,200}", re.IGNORECASE),
    re.compile(r'search(?:ed)?\s+for\s+"[^"]{0,200}"', re.IGNORECASE),
    re.compile(r"search(?:ed)?\s+for\s+[^\n<]{1,200}", re.IGNORECASE),
    re.compile(r"your search[^.\n]{0,120}", re.IGNORECASE),
)
"""Echoes of the agent's own query are not evidence.

A results page prints back the string the agent typed. Matching that would let
any search "verify" itself, which is exactly the false-success mode the guide
forbids. Removing the echo first forces the sought value to appear somewhere
the page authored itself.
"""


class VerificationError(RuntimeError):
    """Raised when verification cannot run at all (never treated as success)."""


@runtime_checkable
class Verifier(Protocol):
    async def verify(self, *, goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult: ...


def strip_echoes(text: str) -> str:
    """Remove search-result echoes of the agent's own query."""
    result = text
    for pattern in _ECHO_PATTERNS:
        result = pattern.sub(" ", result)
    return result


def extract_expectations(goal: str) -> tuple[str, ...]:
    """Quoted phrases and "find X" targets that a page should literally show."""
    phrases: list[str] = []
    for match in re.finditer(r'"([^"]{2,120})"|“([^”]{2,120})”', goal):
        value = (match.group(1) or match.group(2) or "").strip()
        if value:
            phrases.append(value)
    for match in re.finditer(
        r"\b(?:find|show|locate|extract|confirm|verify|check)\b"
        r"[^.\n]{0,20}?\b(?:that|whether|the value|the text)\b[:\s]+([^\n.]{2,120})",
        goal,
        flags=re.IGNORECASE,
    ):
        value = match.group(1).strip()
        if value:
            phrases.append(value)
    return tuple(dict.fromkeys(phrases))


@dataclass
class RuleBasedVerifier:
    """Deterministic evidence rules over a page observation.

    Rules, in order:

    1. Every explicit phrase extracted from the goal must appear in the final
       page evidence after search-query echoes are removed.
    2. A goal without an explicit phrase is not verified by word overlap alone;
       callers should provide a task-specific :class:`CallableVerifier`.
    3. A page that reports not-found/error language never verifies.
    """

    require_change: bool = True
    min_token_overlap: float = 0.6
    not_found_markers: tuple[str, ...] = _NEGATIONS

    async def verify(self, *, goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult:
        return self.verify_sync(goal=goal, before=before, after=after)

    def verify_sync(self, *, goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult:
        if not goal.strip():
            return VerificationResult(verified=False, detail="no goal to verify")
        haystack = strip_echoes(f"{after.title}\n{after.excerpt}").lower()
        if not haystack.strip():
            return VerificationResult(verified=False, detail="the final page had no readable content")
        for marker in self.not_found_markers:
            if marker in haystack:
                return VerificationResult(
                    verified=False,
                    detail=f"the final page reports a failure marker ({marker!r})",
                )
        if self.require_change and after.page_fingerprint == before.page_fingerprint:
            return VerificationResult(verified=False, detail="the page did not change, so the goal cannot be confirmed")
        # An echo of the query never counts, but a real value the page
        # authored does. `require_change` already rejects a page that did not
        # move at all, so no separate "was it there before" veto is needed:
        # that veto caused false negatives when the goal was genuinely
        # satisfied on the first observation.
        expectations = extract_expectations(goal)
        evidence: list[EvidenceRef] = [
            EvidenceRef(kind="snapshot", ref=after.snapshot_id, detail=after.url),
        ]
        if not expectations:
            goal_tokens = _tokens(goal)
            overlap = goal_tokens & _tokens(haystack)
            if goal_tokens and len(overlap) / len(goal_tokens) >= self.min_token_overlap:
                evidence.append(
                    EvidenceRef(
                        kind="tokens",
                        ref=",".join(sorted(overlap))[:200],
                        detail="diagnostic overlap only; it does not verify task success",
                    )
                )
            return VerificationResult(
                verified=False,
                detail="no explicit verification target could be extracted; use a task-specific verifier",
                evidence=tuple(evidence),
            )
        missing = [phrase for phrase in expectations if phrase.lower() not in haystack]
        evidence.extend(
            EvidenceRef(kind="phrase", ref=phrase[:120], detail="found on the final page")
            for phrase in expectations
            if phrase not in missing
        )
        if missing:
            return VerificationResult(
                verified=False,
                detail="explicit evidence missing: " + ", ".join(phrase[:80] for phrase in missing),
                evidence=tuple(evidence),
            )
        return VerificationResult(
            verified=True,
            detail=f"all {len(expectations)} explicit verification phrases are present on the final page",
            evidence=tuple(evidence),
        )


@dataclass
class CallableVerifier:
    """Host-supplied verification, for a future Sentinel or human reviewer."""

    func: Callable[[str, PageSnapshot, PageSnapshot], Awaitable[VerificationResult]]
    name: str = "callable"

    async def verify(self, *, goal: str, before: PageSnapshot, after: PageSnapshot) -> VerificationResult:
        result = await self.func(goal, before, after)
        if not isinstance(result, VerificationResult):
            raise VerificationError("the verifier returned an unexpected value")
        return result


async def verify_outcome(
    verifier: Verifier, *, goal: str, before: PageSnapshot, after: PageSnapshot
) -> VerificationResult:
    """Call a verifier, converting any failure into an explicit non-verified result."""
    try:
        result = await verifier.verify(goal=goal, before=before, after=after)
    except VerificationError:
        return VerificationResult(verified=False, detail="verification could not run")
    except Exception as error:  # noqa: BLE001 - verification errors are never success
        return VerificationResult(verified=False, detail=f"verification failed ({type(error).__name__})")
    if not isinstance(result, VerificationResult):
        return VerificationResult(verified=False, detail="the verifier returned an invalid result")
    return result


def goal_revision_of(_: Any) -> int:
    return 0
