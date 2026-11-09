"""Shared test fixtures: deterministic offline transports and snapshots."""

from __future__ import annotations

import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import pytest

from jev_browser_py.action_space import RawElement, build_action_space
from jev_browser_py.contracts import ActionSpace, PageSnapshot
from jev_browser_py.questions import build_criteria
from jev_browser_py.transport import MockTransport, TransportReply


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
