"""Example: judgment-only use with a real TypeSafe API key.

This is the seam a future aiagent-md adapter uses: propose one action, but
never touch the page. Execution belongs to whoever owns the browser.

    TYPESAFE_API_KEY=... python examples/judgment_only.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from fixture_server import fixture_server  # noqa: E402
from jev_browser_py import TypeSafeTransport  # noqa: E402
from jev_browser_py.runtime import suggest_next_action  # noqa: E402
from jev_browser_py.session import NetworkPolicy, open_session  # noqa: E402
from jev_browser_py.snapshot import Snapshotter  # noqa: E402


async def main() -> None:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is required for this example", file=sys.stderr)
        raise SystemExit(2)
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))
            try:
                snapshot = await Snapshotter(page).observe()
                transport = TypeSafeTransport(api_key=api_key)
                try:
                    proposal = await suggest_next_action(
                        'find the phrase "Annual Report 2026"',
                        snapshot=snapshot,
                        transport=transport,
                    )
                finally:
                    await transport.aclose()
                print(f"proposed action: {proposal.action_key}")
                print(f"side effect:     {proposal.side_effect.value}")
                print(f"snapshot:        {proposal.snapshot_id}")
                print("\nThis is a proposal only. Nothing was clicked; the caller owns execution.")
            finally:
                await session.close()
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
