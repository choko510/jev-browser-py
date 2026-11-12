"""Structured extraction from HTML the browser already fetched.

The rule for v1.0 is absolute: this module never performs a second fetch and
never launches its own browser. It hands an existing HTML string to Scrapling's
`Selector` and attaches provenance (URL, observation time, selector text, and
a SHA-256 of the source) to every result.

Why: a second fetch would bypass the browser's cookies, the network guard, and
the side-effect ledger, and would silently read a different page than the one
the agent acted on.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ErrorCode, Evidence, JevBrowserError

__all__ = [
    "SOURCE_MAX_BYTES",
    "ExtractionError",
    "ExtractionResult",
    "SelectorSpec",
    "extract_html",
    "extract_structured",
]

SOURCE_MAX_BYTES = 8_000_000
VALUE_MAX_CHARS = 2_000
MAX_VALUES = 500


class ExtractionError(JevBrowserError):
    code = ErrorCode.TRANSPORT_FAILED


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """One named extraction request."""

    name: str
    selector: str
    mode: str = "text"
    attribute: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a selector name must not be empty")
        if not self.selector.strip():
            raise ValueError("a selector must not be empty")
        if self.mode not in {"text", "attr", "html"}:
            raise ValueError("mode must be one of text, attr, html")
        if self.mode == "attr" and not self.attribute:
            raise ValueError("attr mode requires an attribute name")
        if self.limit <= 0 or self.limit > MAX_VALUES:
            raise ValueError("limit must be within 1..500")

    def scrapling_selector(self) -> str:
        if self.mode == "text":
            return f"{self.selector}::text"
        if self.mode == "attr":
            return f"{self.selector}::attr({self.attribute})"
        return self.selector


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Values plus provenance; `source_sha256` binds them to one HTML string."""

    url: str
    extracted_at: float
    source_sha256: str
    values: dict[str, tuple[str, ...]]
    selectors: dict[str, str]
    source_bytes: int
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    def as_evidence(self) -> tuple[Evidence, ...]:
        return (
            Evidence(
                url=self.url,
                extracted_at=self.extracted_at,
                selector="; ".join(f"{name}={selector}" for name, selector in self.selectors.items()),
                source_sha256=self.source_sha256,
                values=tuple(value for values in self.values.values() for value in values)[:MAX_VALUES],
                truncated=self.truncated,
            ),
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _import_selector() -> Any:
    try:
        from scrapling.parser import Selector
    except ImportError as error:  # pragma: no cover - declared dependency
        raise ExtractionError("scrapling is not installed; structured extraction is unavailable") from error
    return Selector


def _clean(value: Any) -> str:
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:VALUE_MAX_CHARS]


def extract_html(html: str, *, url: str = "") -> Any:
    """Wrap existing HTML in a Scrapling tree without fetching anything."""
    Selector = _import_selector()
    if len(html.encode("utf-8", errors="replace")) > SOURCE_MAX_BYTES:
        raise ExtractionError(f"the HTML source exceeds the {SOURCE_MAX_BYTES} byte extraction limit")
    try:
        return Selector(content=html, url=url)
    except Exception as error:
        raise ExtractionError("the HTML could not be parsed") from error


def extract_structured(
    html: str,
    *,
    url: str = "",
    specs: Sequence[SelectorSpec],
    extracted_at: float | None = None,
) -> ExtractionResult:
    """Extract named values from already-fetched HTML, with provenance.

    No network access occurs here. If a selector is invalid, that selector
    yields no values and a warning is recorded rather than failing the whole
    extraction, because one bad CSS string should not discard good evidence.
    """
    if not specs:
        raise ExtractionError("at least one selector is required")
    tree = extract_html(html, url=url)
    values: dict[str, tuple[str, ...]] = {}
    selectors: dict[str, str] = {}
    warnings: list[str] = []
    truncated = False
    for spec in specs:
        selectors[spec.name] = spec.scrapling_selector()
        try:
            matched = tree.css(spec.scrapling_selector())
            raw_values = matched.getall() if hasattr(matched, "getall") else []
        except Exception as error:  # noqa: BLE001 - one bad selector is isolated
            warnings.append(f"selector {spec.name!r} could not be evaluated ({type(error).__name__})")
            values[spec.name] = ()
            continue
        cleaned = tuple(_clean(value) for value in raw_values if str(value).strip())
        if len(cleaned) > spec.limit:
            truncated = True
            cleaned = cleaned[: spec.limit]
        values[spec.name] = cleaned
    return ExtractionResult(
        url=url,
        extracted_at=extracted_at if extracted_at is not None else time.time(),
        source_sha256=_sha256(html),
        values=values,
        selectors=selectors,
        source_bytes=len(html.encode("utf-8", errors="replace")),
        truncated=truncated,
        warnings=tuple(warnings),
    )


def extract_links(html: str, *, url: str = "", limit: int = 50) -> ExtractionResult:
    """Common case: page title and link hrefs."""
    return extract_structured(
        html,
        url=url,
        specs=[
            SelectorSpec(name="title", selector="title", mode="text", limit=1),
            SelectorSpec(name="links", selector="a", mode="attr", attribute="href", limit=limit),
        ],
    )


def values_for(result: ExtractionResult, name: str) -> tuple[str, ...]:
    return result.values.get(name, ())


def verify_against_page(result: ExtractionResult, page_excerpt: str) -> bool:
    """Cross-check that extracted text appears in the observed page excerpt.

    A cheap consistency guard: if extraction and observation disagree, the
    evidence is not trustworthy and the caller should not report it as fact.
    """
    joined = " ".join(value for values in result.values.values() for value in values)
    if not joined.strip() or not page_excerpt.strip():
        return False
    sample = joined.split()[:10]
    return all(word in page_excerpt for word in sample)


def merge_results(results: Iterable[ExtractionResult]) -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {}
    for result in results:
        for name, values in result.values.items():
            merged.setdefault(name, []).extend(values)
    return {name: tuple(values[:MAX_VALUES]) for name, values in merged.items()}
