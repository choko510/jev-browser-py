"""Shared test fixtures: deterministic offline transports and snapshots."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from jev_browser_py.action_space import RawElement, build_action_space
from jev_browser_py.contracts import ActionSpace, PageSnapshot
from jev_browser_py.questions import build_criteria
from jev_browser_py.transport import MockTransport, TransportReply


def browser_install_path() -> Path:
    """Resolve Playwright's browser store exactly as the runtime environment does."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        if configured == "0":
            try:
                import playwright
            except ImportError:
                return Path()
            return Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return root / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def playwright_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def chromium_installed() -> bool:
    try:
        return any(path.name.startswith("chromium") for path in browser_install_path().iterdir())
    except OSError:
        return False


def browser_test_marks() -> list[Any]:
    """Skip locally when unavailable, but never skip CI-required browser tests."""
    marks: list[Any] = [pytest.mark.browser]
    if os.environ.get("JEV_REQUIRE_BROWSER_TESTS") == "1":
        return marks
    if not playwright_available():
        marks.append(pytest.mark.skipif(True, reason="playwright is not installed"))
    if not chromium_installed():
        marks.append(pytest.mark.skipif(True, reason="no local Chromium build for Playwright"))
    return marks


_BROWSER_REQUIRED_PASSES = 0


def pytest_runtest_logreport(report: Any) -> None:
    global _BROWSER_REQUIRED_PASSES
    if (
        os.environ.get("JEV_REQUIRE_BROWSER_TESTS") == "1"
        and report.when == "call"
        and report.passed
        and "browser" in report.keywords
    ):
        _BROWSER_REQUIRED_PASSES += 1


def pytest_sessionfinish(session: Any, exitstatus: pytest.ExitCode) -> None:
    if (
        os.environ.get("JEV_REQUIRE_BROWSER_TESTS") == "1"
        and session.config.getoption("markexpr") == "browser"
        and exitstatus == pytest.ExitCode.OK
        and _BROWSER_REQUIRED_PASSES == 0
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def make_snapshot(
    *,
    snapshot_id: str = "snap-1",
    generation: int = 0,
    url: str = "https://example.test/",
    title: str = "Example",
    excerpt: str = "A small fixture page.",
    action_space: ActionSpace | None = None,
    truncated: bool = False,
    page_fingerprint: str | None = None,
) -> PageSnapshot:
    import hashlib

    fingerprint = page_fingerprint
    if fingerprint is None:
        digest = hashlib.sha256(f"{url}|{title}|{excerpt}".encode()).hexdigest()
        fingerprint = digest[:24]
    return PageSnapshot(
        snapshot_id=snapshot_id,
        generation=generation,
        url=url,
        title=title,
        observed_at=time.time(),
        excerpt=excerpt,
        frame_ref=None,
        action_space=action_space or build_action_space([]),
        truncated=truncated,
        html_bytes=len(excerpt.encode("utf-8")),
        page_fingerprint=fingerprint,
    )


def sample_elements() -> list[RawElement]:
    """A small, realistic page: link, button, search box, select, text field."""
    return [
        RawElement(
            attr="j1",
            tag="a",
            role="a",
            text="Documentation",
            href="https://example.test/docs",
            clickable=True,
        ),
        RawElement(
            attr="j2",
            tag="button",
            role="button",
            text="Continue",
            clickable=True,
        ),
        RawElement(
            attr="j3",
            tag="input",
            role="searchbox",
            text="Search the site",
            type_attr="search",
            typeable=True,
            search_field=True,
        ),
        RawElement(
            attr="j4",
            tag="select",
            role="select",
            text="Region",
            selectable=True,
            options=((0, "EMEA"), (1, "APAC"), (2, "Americas")),
        ),
        RawElement(
            attr="j5",
            tag="input",
            role="textbox",
            text="Email address",
            type_attr="text",
            typeable=True,
            enter_submittable=True,
        ),
        RawElement(
            attr="j6",
            tag="a",
            role="a",
            text="Docs permalink",
            href="https://example.test/docs#install",
            clickable=True,
        ),
        RawElement(
            attr="j7",
            tag="input",
            role="input",
            text="Password",
            type_attr="password",
            password_input=True,
        ),
    ]


def sample_action_space() -> ActionSpace:
    return build_action_space(sample_elements())


def snapshot_with_actions(**kwargs: Any) -> PageSnapshot:
    """Snapshot whose candidates carry real stamps (jN), as the browser sets."""
    return make_snapshot(action_space=sample_action_space(), **kwargs)


def action_answer(
    criteria: Mapping[str, str],
    choice: str,
    *,
    confidence: float = 0.9,
    spread: float = 0.5,
) -> dict[str, Any]:
    """Build a valid Choice answer whose peak is `choice`."""
    others = [key for key in criteria if key != choice]
    if not others:
        probabilities = {choice: 1.0}
    else:
        peak = max(0.05, min(1.0 - 1e-6, spread))
        remainder = (1.0 - peak) / len(others)
        probabilities = dict.fromkeys(others, remainder)
        probabilities[choice] = peak
        total = sum(probabilities.values())
        probabilities = {key: value / total for key, value in probabilities.items()}
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }


def step_reply(
    action_space: ActionSpace,
    choice: str,
    *,
    goal_done: float = 0.02,
    stuck: float = 0.02,
    confidence: float = 0.9,
    spread: float = 0.5,
    **overrides: Any,
) -> dict[str, Any]:
    criteria = build_criteria(action_space)
    if choice not in criteria:
        raise AssertionError(f"{choice} is not in {sorted(criteria)}")
    answers: dict[str, Any] = {
        "action": action_answer(criteria, choice, confidence=confidence, spread=spread),
        "goal_done": {"type": "noul", "noul": goal_done},
        "stuck": {"type": "noul", "noul": stuck},
    }
    answers.update(overrides)
    return answers


@pytest.fixture
def snapshot() -> PageSnapshot:
    return make_snapshot(action_space=sample_action_space())


@pytest.fixture
def space() -> ActionSpace:
    return sample_action_space()


def transport_from(*replies: Mapping[str, Any], **kwargs: Any) -> MockTransport:
    return MockTransport(replies=list(replies), **kwargs)


def reply_object(answers: Mapping[str, Any], *, model: str = "jev-mock-1") -> TransportReply:
    return TransportReply(
        answers=answers,
        model=model,
        provider="mock",
        input_tokens=100,
        output_tokens=10,
    )


@lru_cache(maxsize=1)
def _cached_criteria_keys() -> tuple[str, ...]:
    return tuple(build_criteria(sample_action_space()))
