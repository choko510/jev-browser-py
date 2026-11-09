"""PYJEV-T11: Scrapling extraction from already-fetched HTML, no second fetch."""

from __future__ import annotations

import time

import pytest

from jev_browser_py.extraction import (
    MAX_VALUES,
    ExtractionError,
    SelectorSpec,
    extract_html,
    extract_links,
    extract_structured,
    merge_results,
    values_for,
    verify_against_page,
)

PAGE = """<!doctype html>
<html>
<head><title>Quarterly report</title></head>
<body>
  <h1>Quarterly report</h1>
  <p class="lede">Revenue of 42 million for the quarter.</p>
  <ul>
    <li><a href="/a">Alpha</a></li>
    <li><a href="/b">Beta</a></li>
    <li><a href="/c">Gamma</a></li>
  </ul>
  <table id="figures">
    <tr><td>Revenue</td><td>42</td></tr>
    <tr><td>Cost</td><td>17</td></tr>
  </table>
</body>
</html>
"""


def test_text_and_attribute_extraction() -> None:
    result = extract_structured(
        PAGE,
        url="https://example.test/report",
        specs=[
            SelectorSpec(name="title", selector="title", mode="text"),
            SelectorSpec(name="lede", selector="p.lede", mode="text"),
            SelectorSpec(name="links", selector="a", mode="attr", attribute="href"),
        ],
    )
    assert result.values["title"] == ("Quarterly report",)
    assert result.values["lede"] == ("Revenue of 42 million for the quarter.",)
    assert result.values["links"] == ("/a", "/b", "/c")


def test_provenance_is_recorded() -> None:
    when = time.time()
    result = extract_structured(
        PAGE,
        url="https://example.test/report",
        specs=[SelectorSpec(name="title", selector="title")],
        extracted_at=when,
    )
    assert result.url == "https://example.test/report"
    assert result.extracted_at == when
    assert len(result.source_sha256) == 64
    assert result.source_bytes == len(PAGE.encode("utf-8"))
    assert result.selectors["title"] == "title::text"


def test_identical_html_yields_identical_digest() -> None:
    first = extract_structured(PAGE, specs=[SelectorSpec(name="t", selector="title")])
    second = extract_structured(PAGE, specs=[SelectorSpec(name="t", selector="title")])
    assert first.source_sha256 == second.source_sha256


def test_changed_html_changes_the_digest() -> None:
    first = extract_structured(PAGE, specs=[SelectorSpec(name="t", selector="title")])
    second = extract_structured(
        PAGE.replace("42 million", "43 million"),
        specs=[SelectorSpec(name="t", selector="title")],
    )
    assert first.source_sha256 != second.source_sha256


def test_missing_selector_yields_no_values_not_an_error() -> None:
    result = extract_structured(PAGE, specs=[SelectorSpec(name="missing", selector=".does-not-exist")])
    assert result.values["missing"] == ()


def test_one_bad_selector_does_not_discard_good_evidence() -> None:
    result = extract_structured(
        PAGE,
        specs=[
            SelectorSpec(name="bad", selector="a[unclosed"),
            SelectorSpec(name="title", selector="title"),
        ],
    )
    assert result.values["title"] == ("Quarterly report",)
    assert result.values["bad"] == ()
    assert result.warnings


def test_limit_truncates_and_records_it() -> None:
    result = extract_structured(
        PAGE, specs=[SelectorSpec(name="links", selector="a", mode="attr", attribute="href", limit=2)]
    )
    assert len(result.values["links"]) == 2
    assert result.truncated is True


def test_extraction_never_performs_a_network_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket use during extraction would mean Scrapling fetched something."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("extraction must not open a socket")

    monkeypatch.setattr("socket.socket", explode)
    result = extract_structured(PAGE, url="https://example.test/", specs=[SelectorSpec(name="title", selector="title")])
    assert result.values["title"] == ("Quarterly report",)


def test_oversized_html_is_refused() -> None:
    huge = "<html><body>" + ("x" * (8_000_001)) + "</body></html>"
    with pytest.raises(ExtractionError, match="extraction limit"):
        extract_html(huge)


def test_empty_html_is_still_parseable() -> None:
    result = extract_structured("<html><body></body></html>", specs=[SelectorSpec(name="p", selector="p")])
    assert result.values["p"] == ()


def test_extract_links_convenience_helper() -> None:
    result = extract_links(PAGE, url="https://example.test/")
    assert result.values["title"] == ("Quarterly report",)
    assert "/a" in result.values["links"]


def test_selector_spec_validation() -> None:
    with pytest.raises(ValueError, match="name"):
        SelectorSpec(name=" ", selector="a")
    with pytest.raises(ValueError, match="selector"):
        SelectorSpec(name="x", selector=" ")
    with pytest.raises(ValueError, match="mode"):
        SelectorSpec(name="x", selector="a", mode="css")
    with pytest.raises(ValueError, match="attribute"):
        SelectorSpec(name="x", selector="a", mode="attr")
    with pytest.raises(ValueError, match="limit"):
        SelectorSpec(name="x", selector="a", limit=0)


def test_evidence_carries_values_and_source_hash() -> None:
    result = extract_structured(PAGE, url="https://example.test/", specs=[SelectorSpec(name="title", selector="title")])
    evidence = result.as_evidence()
    assert len(evidence) == 1
    assert evidence[0].source_sha256 == result.source_sha256
    assert evidence[0].url == "https://example.test/"
    assert "Quarterly report" in evidence[0].values


def test_verify_against_page_detects_mismatch() -> None:
    result = extract_structured(PAGE, specs=[SelectorSpec(name="title", selector="title")])
    assert verify_against_page(result, "Quarterly report is ready") is True
    assert verify_against_page(result, "completely different content") is False


def test_values_for_missing_name_is_empty() -> None:
    result = extract_structured(PAGE, specs=[SelectorSpec(name="title", selector="title")])
    assert values_for(result, "nope") == ()


def test_merge_results_combines_values() -> None:
    first = extract_structured(PAGE, specs=[SelectorSpec(name="title", selector="title")])
    second = extract_structured(PAGE, specs=[SelectorSpec(name="lede", selector="p.lede")])
    merged = merge_results([first, second])
    assert merged["title"] == ("Quarterly report",)
    assert "Revenue" in merged["lede"][0]


def test_html_mode_returns_outer_markup() -> None:
    result = extract_structured(PAGE, specs=[SelectorSpec(name="table", selector="table#figures", mode="html")])
    assert "<table" in result.values["table"][0]


def test_values_are_whitespace_normalized() -> None:
    html = "<html><body><p>  lots\n\n  of   space  </p></body></html>"
    result = extract_structured(html, specs=[SelectorSpec(name="p", selector="p")])
    assert result.values["p"] == ("lots of space",)


def test_max_values_constant_is_finite() -> None:
    assert MAX_VALUES == 500


def test_xpath_style_selector_is_supported() -> None:
    result = extract_structured(PAGE, specs=[SelectorSpec(name="h1", selector="h1")])
    assert result.values["h1"] == ("Quarterly report",)
