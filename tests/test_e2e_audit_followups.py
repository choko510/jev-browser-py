"""Real-Chromium regressions for the follow-up audit findings.

These prove the two security-critical seams against actual Playwright, not
fakes: a shared Context cannot enforce two different network policies, and two
links whose query values redact to the same text remain two distinct actions
without either value reaching model-facing text.
"""

from __future__ import annotations

import pytest

from conftest import browser_test_marks
from fixture_server import fixture_server
from jev_browser_py.contracts import NetworkGuardError
from jev_browser_py.session import NetworkPolicy, open_session
from jev_browser_py.snapshot import Snapshotter

pytestmark = browser_test_marks()

DISTINCT_LINKS = """<!doctype html>
<html><head><title>Distinct links</title></head>
<body>
  <a id="first" href="/detail?record=alpha-secret-value">Alpha record</a>
  <a id="second" href="/detail?record=beta-secret-value">Beta record</a>
  <a id="dup" href="/detail?record=alpha-secret-value">Alpha record duplicate</a>
</body></html>
"""


async def test_a_shared_context_rejects_a_second_network_policy() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            first_page = await context.new_page()
            second_page = await context.new_page()
            try:
                first = await open_session(
                    page=first_page,
                    network=NetworkPolicy(allowed_origins=(base,), allow_loopback=True),
                )
                with pytest.raises(NetworkGuardError, match="already enforces a different network policy"):
                    await open_session(
                        page=second_page,
                        network=NetworkPolicy(allowed_origins=("http://other.example",)),
                    )
                await first.close()
            finally:
                await context.close()
                await browser.close()


async def test_a_shared_context_accepts_the_same_policy_for_two_pages() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            first_page = await context.new_page()
            second_page = await context.new_page()
            policy = NetworkPolicy(allowed_origins=(base,), allow_loopback=True)
            try:
                first = await open_session(page=first_page, network=policy)
                second = await open_session(page=second_page, network=policy)
                await first.navigate(f"{base}/")
                await second.navigate(f"{base}/detail")
                assert first.page.url.startswith(base)
                assert second.page.url.startswith(base)
                await first.close()
                await second.close()
            finally:
                await context.close()
                await browser.close()


async def test_distinct_masked_links_stay_distinct_and_leak_nothing() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(DISTINCT_LINKS)
            snapshot = await Snapshotter(page).observe()

            descriptions = [candidate.description for candidate in snapshot.action_space.candidates]
            assert len(descriptions) == 2, descriptions
            assert any("Alpha record" in text for text in descriptions)
            assert any("Beta record" in text for text in descriptions)

            model_text = " ".join(descriptions)
            assert "alpha-secret-value" not in model_text
            assert "beta-secret-value" not in model_text
        finally:
            await browser.close()
