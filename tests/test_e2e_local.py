"""PYJEV-T04/T11/T16: real Chromium, local fixtures, no network, no API key.

These tests are the honest end-to-end proof: a real browser navigates a local
HTTP fixture, the real snapshotter observes it, a scripted transport makes the
decisions a model would make from the real criteria, the real executor clicks,
and the real verifier confirms the outcome. Nothing is mocked except judgment.

Skipped automatically when no Chromium build is installed.
"""

from __future__ import annotations

from collections.abc import Mapping

from conftest import browser_test_marks
from fixture_server import RESULT_BODY, RESULT_TITLE, fixture_server
from jev_browser_py.contracts import RunLimits, RunStatus
from jev_browser_py.executor import ExecutorOptions, SingleActionExecutor
from jev_browser_py.extraction import SelectorSpec, extract_structured
from jev_browser_py.runtime import RunOptions, run
from jev_browser_py.session import NetworkPolicy, open_session
from jev_browser_py.snapshot import Snapshotter
from jev_browser_py.transport import TransportReply
from jev_browser_py.verifier import RuleBasedVerifier

pytestmark = browser_test_marks()


class CriteriaTransport:
    """Scripted transport that decides from the live criteria list."""

    name = "scripted-e2e"

    def __init__(self, picker: object) -> None:
        self._picker = picker
        self.calls = 0
        self.criteria_seen: list[dict[str, str]] = []

    def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float:
        del request
        return 0.0

    async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
        del deadline
        self.calls += 1
        criteria = dict(questions["action"].criteria)  # type: ignore[index]
        self.criteria_seen.append(criteria)
        action = self._picker(self.calls, criteria)  # type: ignore[operator]
        answers = {
            "action": _choice(criteria, action),
            "goal_done": {"type": "noul", "noul": 0.01},
            "stuck": {"type": "noul", "noul": 0.01},
        }
        return TransportReply(
            answers=answers,
            model="scripted-e2e",
            provider="scripted-e2e",
            input_tokens=120,
            output_tokens=8,
        )


def _choice(criteria: Mapping[str, str], picked: str) -> dict[str, object]:
    if picked not in criteria:
        raise AssertionError(f"{picked} is not among {sorted(criteria)}")
    others = [key for key in criteria if key != picked]
    share = 0.02 / len(others) if others else 0.0
    probabilities = dict.fromkeys(others, share)
    probabilities[picked] = 0.98
    total = sum(probabilities.values())
    return {
        "type": "choice",
        "choice": picked,
        "confidence": 0.9,
        "probabilities": {key: value / total for key, value in probabilities.items()},
    }


def _find(criteria: Mapping[str, str], prefix: str, *, containing: str | None = None) -> str:
    for key, description in criteria.items():
        if key.startswith(prefix) and (containing is None or containing in description):
            return key
    raise AssertionError(f"no {prefix}* action matching {containing!r} in {sorted(criteria)}")


async def test_injected_page_is_never_closed() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))
            try:
                snapshot = await Snapshotter(session.page).observe()
                assert snapshot.url.startswith("http://127.0.0.1:")
                assert len(snapshot.action_space) > 0
            finally:
                await session.close()
                assert page.is_closed() is False, "an injected page must never be closed"
                await context.close()
                await browser.close()


async def test_snapshot_offers_the_real_controls() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            try:
                snapshot = await Snapshotter(page).observe()
                kinds = {candidate.kind.value for candidate in snapshot.action_space.candidates}
                assert "search" in kinds
                assert "submit" in kinds
                descriptions = " ".join(candidate.description for candidate in snapshot.action_space.candidates)
                assert "Search documents" in descriptions
                assert "Nothing has been searched yet." not in descriptions
            finally:
                await browser.close()


async def test_full_local_task_reaches_a_verified_result() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))
            try:

                def picker(call: int, criteria: Mapping[str, str]) -> str:
                    if call == 1:
                        return _find(criteria, "search_")
                    if call == 2:
                        return _find(criteria, "submit_", containing="submit the search now")
                    if call == 3:
                        return _find(criteria, "click_", containing="Annual Report 2026")
                    return "done"

                transport = CriteriaTransport(picker)
                executor = SingleActionExecutor(page, options=ExecutorOptions())
                result = await run(
                    'find the phrase "revenue of 42 million" and report it',
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=executor,
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=6, max_seconds=60, max_jev_calls=8),
                    options=RunOptions(),
                )
                assert result.status is RunStatus.SUCCESS, result.reason
                assert result.verified is True
                assert result.steps >= 3
                assert transport.calls >= 3
                assert page.url.endswith("/detail")

                html = await page.content()
                extracted = extract_structured(
                    html,
                    url=page.url,
                    specs=[
                        SelectorSpec(name="title", selector="title"),
                        SelectorSpec(name="body", selector="#body"),
                    ],
                )
                assert extracted.values["title"] == (RESULT_TITLE,)
                body_values = extracted.values["body"]
                assert body_values and RESULT_BODY in body_values[0]
                assert len(extracted.source_sha256) == 64
            finally:
                await session.close()
                await browser.close()


async def test_read_link_javascript_post_is_blocked_by_action_side_effect() -> None:
    from playwright.async_api import async_playwright

    from jev_browser_py.policy import default_policy

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            session = await open_session(
                page=page,
                network=NetworkPolicy(
                    allowed_origins=(base,), allow_loopback=True, allow_unsafe_methods=True
                ),
            )
            try:
                await session.navigate(f"{base}/js-post")
                transport = CriteriaTransport(
                    lambda _call, criteria: _find(criteria, "click_", containing="Open preferences")
                )
                result = await run(
                    "go to account preferences",
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=SingleActionExecutor(page),
                    policy=default_policy(),
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=1, max_seconds=10, max_jev_calls=1),
                )
                assert result.verified is False
                assert await page.locator("#status").text_content() == "blocked"
            finally:
                await session.close()
                await context.close()
                await browser.close()


async def test_explicit_write_authorization_allows_post_form() -> None:
    from playwright.async_api import async_playwright

    from jev_browser_py.policy import default_policy

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            session = await open_session(
                page=page,
                network=NetworkPolicy(
                    allowed_origins=(base,), allow_loopback=True, allow_unsafe_methods=True
                ),
            )
            try:
                await session.navigate(f"{base}/post-form")
                transport = CriteriaTransport(
                    lambda _call, criteria: next(
                        key for key, description in criteria.items()
                        if key.startswith("submit_") and "submit the form now" in description
                    )
                )
                result = await run(
                    'submit the form and find the phrase "stored"',
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=SingleActionExecutor(page),
                    policy=default_policy(allow_external_write=True),
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=1, max_seconds=10, max_jev_calls=1),
                )
                assert result.status is RunStatus.SUCCESS, result.reason
                assert await page.locator("body").inner_text() == "stored"
            finally:
                await session.close()
                await context.close()
                await browser.close()


async def test_local_task_reports_unverified_when_the_goal_is_absent() -> None:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))
            try:

                def picker(call: int, criteria: Mapping[str, str]) -> str:
                    if call <= 3:
                        return _find(criteria, "search_") if call == 1 else "scroll_down"
                    return "done"

                transport = CriteriaTransport(picker)
                result = await run(
                    'find the phrase "quantum flux capacitor"',
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=SingleActionExecutor(page),
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=4, max_seconds=60, max_jev_calls=8),
                    options=RunOptions(),
                )
                assert result.verified is False
                assert result.status is not RunStatus.SUCCESS
            finally:
                await session.close()
                await browser.close()
