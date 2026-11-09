"""End-to-end example: a complete run against the local fixture server.

This is the example a new user should read first. It runs entirely offline:
a loopback HTTP fixture, a scripted transport that behaves like a model, and
the real snapshotter/executor/verifier/extractor.

    python examples/local_task.py

A real deployment replaces `ScriptedTransport` with:

    from jev_browser_py import TypeSafeTransport
    transport = TypeSafeTransport(api_key=os.environ["TYPESAFE_API_KEY"])
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from fixture_server import fixture_server  # noqa: E402
from jev_browser_py.contracts import RunLimits, RunStatus  # noqa: E402
from jev_browser_py.executor import SingleActionExecutor  # noqa: E402
from jev_browser_py.extraction import SelectorSpec, extract_structured  # noqa: E402
from jev_browser_py.runtime import RunOptions, run  # noqa: E402
from jev_browser_py.session import NetworkPolicy, open_session  # noqa: E402
from jev_browser_py.snapshot import Snapshotter  # noqa: E402
from jev_browser_py.transport import TransportReply  # noqa: E402
from jev_browser_py.verifier import RuleBasedVerifier  # noqa: E402


class DemoTransport:
    """Behaves like a model: searches, then clicks the matching result."""

    name = "demo"

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
        del state, deadline
        self.calls += 1
        criteria = dict(questions["action"].criteria)  # type: ignore[index]
        if self.calls == 1:
            action = next(key for key in criteria if key.startswith("search_"))
        elif self.calls == 2:
            action = next(key for key in criteria if key.startswith("click_") and "Annual Report 2026" in criteria[key])
        else:
            action = "done"
        probabilities: dict[str, float] = dict.fromkeys(criteria, 0.0)
        probabilities[action] = 1.0
        return TransportReply(
            answers={
                "action": {
                    "type": "choice",
                    "choice": action,
                    "confidence": 0.9,
                    "probabilities": probabilities,
                },
                "goal_done": {"type": "noul", "noul": 0.02},
                "stuck": {"type": "noul", "noul": 0.02},
            },
            model="demo-model",
            provider="demo",
            input_tokens=250,
            output_tokens=12,
        )


def pick(criteria: dict[str, str], prefix: str, *, contains: str | None = None) -> str:
    for key, description in criteria.items():
        if key.startswith(prefix) and (contains is None or contains in description):
            return key
    raise SystemExit(f"no {prefix} action matching {contains!r}")


async def main() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))
            try:
                transport = DemoTransport()
                result = await run(
                    'find the phrase "revenue of 42 million" and report it',
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=SingleActionExecutor(page),
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=6, max_seconds=60, max_jev_calls=8),
                    options=RunOptions(),
                )
                print(f"status:   {result.status.value}")
                print(f"verified: {result.verified}")
                print(f"reason:   {result.reason}")
                print(f"steps:    {result.steps}")
                assert result.status is RunStatus.SUCCESS, result.reason

                html = await page.content()
                extracted = extract_structured(
                    html,
                    url=page.url,
                    specs=[
                        SelectorSpec(name="title", selector="title"),
                        SelectorSpec(name="body", selector="#body"),
                    ],
                )
                print("\nextracted evidence:")
                for name, values in extracted.values.items():
                    print(f"  {name}: {values}")
                print(f"\nsource sha256: {extracted.source_sha256}")

                print("\nredacted trace:")
                for step in result.trace:
                    print(f"  step {step.step}: {step.proposed_action} -> {step.executed_action} ({step.outcome})")
            finally:
                await session.close()
                await browser.close()


async def _unused(_: Any) -> None:
    return None


if __name__ == "__main__":
    asyncio.run(main())
