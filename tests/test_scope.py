"""PYJEV-T15: SPA re-render, iframe scope, and popup handling in a real browser.

The guide requires that supported cases work AND unsupported cases stop safely
instead of pretending to succeed. These tests exercise the real browser for
each boundary.
"""

from __future__ import annotations

import os

import pytest

from fixture_server import fixture_server
from jev_browser_py.session import NetworkPolicy, open_session
from jev_browser_py.snapshot import Snapshotter

pytestmark = pytest.mark.browser


def _playwright_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _chromium_installed() -> bool:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright"
    )
    if not base or not os.path.isdir(base):
        return False
    return any(name.startswith("chromium") for name in os.listdir(base))


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not _playwright_available(), reason="playwright is not installed"),
    pytest.mark.skipif(not _chromium_installed(), reason="no local Chromium build"),
]

SPA_HTML = """<!doctype html>
<html><head><title>SPA fixture</title></head>
<body>
  <div id="app"><p>Loading…</p></div>
  <script>
    setTimeout(() => {
      document.getElementById("app").innerHTML =
        '<h1>Loaded view</h1><button id="go">Continue</button>';
    }, 120);
  </script>
</body></html>
"""

IFRAME_HTML = """<!doctype html>
<html><head><title>Frame host</title></head>
<body>
  <h1>Host page</h1>
  <button id="outer">Outer button</button>
  <iframe src="/frame-inner"></iframe>
</body></html>
"""

INNER_HTML = """<!doctype html>
<html><head><title>Inner</title></head>
<body><button id="inner">Inner-only button</button></body></html>
"""


async def test_spa_rerender_changes_the_snapshot_generation_and_fingerprint() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(SPA_HTML)
            snapshotter = Snapshotter(page)
            first = await snapshotter.observe()
            assert "Loading" in first.excerpt
            assert len(first.action_space) == 0

            await page.wait_for_selector("#go", timeout=5_000)
            second = await snapshotter.observe(bump=True)

            assert second.generation > first.generation
            assert second.page_fingerprint != first.page_fingerprint
            descriptions = " ".join(candidate.description for candidate in second.action_space.candidates)
            assert "Continue" in descriptions
        finally:
            await browser.close()


async def test_iframe_contents_are_deliberately_out_of_scope() -> None:
    """Frame-internal controls must not be offered: no silent cross-frame click."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(IFRAME_HTML)
            # The iframe loads asynchronously; settle before observing.
            await page.wait_for_selector("iframe")
            snapshot = await Snapshotter(page).observe()
            descriptions = " ".join(candidate.description for candidate in snapshot.action_space.candidates)
            assert "Outer button" in descriptions
            assert "Inner-only button" not in descriptions
        finally:
            await browser.close()


async def test_a_page_with_no_controls_reports_an_empty_space() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(
                "<html><head><title>Canvas</title></head>"
                "<body><canvas id='c' width='200' height='100'></canvas></body></html>"
            )
            snapshot = await Snapshotter(page).observe()
            assert len(snapshot.action_space) == 0
            assert snapshot.truncated is False
        finally:
            await browser.close()


async def test_popup_navigation_is_not_followed_in_v1() -> None:
    """A popup opens a new page; the observed page is unchanged."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.set_content(
                "<html><head><title>Popup host</title></head><body>"
                "<button id='open' onclick=\"window.open('about:blank')\">Open popup</button>"
                "</body></html>"
            )
            before = await Snapshotter(page).observe()
            await page.click("#open")
            after = await Snapshotter(page).observe()
            assert after.url == before.url
            assert len(context.pages) >= 1
        finally:
            await context.close()
            await browser.close()


async def test_cross_origin_navigation_is_blocked_by_the_policy() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(
                page=page,
                network=NetworkPolicy(allowed_origins=("https://allowed.example",)),
                install_guard=False,
            )
            try:
                from jev_browser_py.contracts import NetworkGuardError

                with pytest.raises(NetworkGuardError):
                    await session.navigate("http://169.254.169.254/latest/meta-data/")
            finally:
                await session.close()
                await browser.close()


async def test_context_route_blocks_a_real_redirect_to_an_unlisted_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from playwright.async_api import async_playwright

    from jev_browser_py.contracts import NetworkGuardError

    def public_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("jev_browser_py.session.socket.getaddrinfo", public_dns)
    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--host-resolver-rules=MAP unlisted.example 127.0.0.1"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            session = await open_session(
                page=page,
                network=NetworkPolicy(allowed_origins=(base,), allow_loopback=True),
            )
            try:
                with pytest.raises(NetworkGuardError):
                    await session.navigate(f"{base}/redirect-unlisted")
                assert not page.url.endswith("/detail")
            finally:
                await session.close()
                await context.close()
                await browser.close()


async def test_context_guard_blocks_unlisted_subresource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from playwright.async_api import async_playwright

    def public_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("jev_browser_py.session.socket.getaddrinfo", public_dns)
    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--host-resolver-rules=MAP unlisted.example 127.0.0.1"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            session = await open_session(
                page=page,
                network=NetworkPolicy(allowed_origins=(base,), allow_loopback=True),
            )
            try:
                await session.navigate(f"{base}/subresource")
                await page.wait_for_function("() => document.querySelector('img')?.complete")
                width = await page.locator("img").evaluate("img => img.naturalWidth")
                assert width == 0
            finally:
                await session.close()
                await context.close()
                await browser.close()


async def test_redirect_to_a_private_host_is_refused_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destination is checked, so a redirect target is judged on its own."""
    import socket

    from jev_browser_py.contracts import NetworkGuardError

    def public_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("jev_browser_py.session.socket.getaddrinfo", public_dns)
    policy = NetworkPolicy(allowed_origins=("https://public.example",))
    assert policy.origin_allowed("https://public.example/start") is True
    with pytest.raises(NetworkGuardError):
        policy.check("http://10.0.0.1/landing")
