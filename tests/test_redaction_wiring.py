"""Regression test for the redactor-attachment bug found during review.

`run(secrets=...)` previously built a redactor and never installed it on the
snapshotter, so page text would have reached the model unredacted while the
API looked protected. These tests fail if that ever regresses.
"""

from __future__ import annotations

import pytest

from conftest import make_snapshot, sample_action_space, step_reply
from jev_browser_py.contracts import RunLimits, RunStatus
from jev_browser_py.redaction import RedactionFailure
from jev_browser_py.runtime import run
from jev_browser_py.snapshot import Snapshotter
from jev_browser_py.transport import MockTransport


class _RecordingSnapshotter:
    redactor = None

    def __init__(self) -> None:
        from jev_browser_py.redaction import Redactor

        self.redactor = Redactor([])
        self.ensured: list[list[str]] = []

    def ensure_secrets(self, secrets: list[str]) -> None:
        from jev_browser_py.redaction import build_redactor

        self.ensured.append(list(secrets))
        self.redactor = build_redactor(secrets)

    def bump_generation(self) -> int:
        return 0

    async def observe(self, *, bump: bool = False):  # type: ignore[no-untyped-def]
        del bump
        return make_snapshot(action_space=sample_action_space())


async def test_run_installs_secrets_on_the_snapshotter() -> None:
    from fakes import FakeExecutor

    snapshotter = _RecordingSnapshotter()
    transport = MockTransport([step_reply(sample_action_space(), "done")])
    await run(
        "find nothing",
        transport=transport,
        snapshotter=snapshotter,  # type: ignore[arg-type]
        executor=FakeExecutor(),
        limits=RunLimits(max_steps=1),
        secrets=["a-real-secret-value"],
    )
    assert snapshotter.ensured == [["a-real-secret-value"]]


async def test_run_without_secrets_leaves_the_snapshotter_alone() -> None:
    from fakes import FakeExecutor

    snapshotter = _RecordingSnapshotter()
    transport = MockTransport([step_reply(sample_action_space(), "done")])
    await run(
        "find nothing",
        transport=transport,
        snapshotter=snapshotter,  # type: ignore[arg-type]
        executor=FakeExecutor(),
        limits=RunLimits(max_steps=1),
    )
    assert snapshotter.ensured == []


def test_snapshotter_registers_and_verifies_secrets() -> None:
    from fakes import FakePage

    snapshotter = Snapshotter(FakePage(), secrets=["initial-secret-value"])
    assert snapshotter.redactor.has_secrets() is True
    snapshotter.ensure_secrets(["another-secret-value"])
    assert "initial-secret-value" not in snapshotter.redactor.redact("x initial-secret-value y")
    assert "another-secret-value" not in snapshotter.redactor.redact("x another-secret-value y")


def test_snapshotter_masks_unknown_url_tokens_in_page_and_link_values() -> None:
    from fakes import FakePage
    from jev_browser_py.action_space import RawElement

    secret = "unknown-auth-token-abcdef1234567890"
    snapshotter = Snapshotter(FakePage())
    safe_url = snapshotter._redact(f"https://example.test/callback?opaque={secret}#token={secret}")
    element = RawElement(
        attr="j1",
        tag="a",
        role="a",
        text=f"Open https://example.test/?unknown={secret}",
        href=f"/account/{secret}?opaque={secret}",
        clickable=True,
    )

    safe_element = snapshotter._redact_element(element)

    assert secret not in safe_url
    assert secret not in safe_element.text
    assert secret not in safe_element.href


def test_model_state_sanitizes_urls_from_task_history_snapshot_and_criteria() -> None:
    from jev_browser_py.action_space import RawElement, build_action_space
    from jev_browser_py.questions import build_criteria, judgment_state, select_option_question

    path_token = "opaque-path-auth-0123456789abcdefghijkl"
    query_token = "unknown-query-token-abcdef"
    fragment_token = "unknown-fragment-token-abcdef"
    url = f"https://example.test/account/{path_token}?mystery={query_token}#state={fragment_token}"
    element = RawElement(
        attr="j1",
        tag="a",
        role="link",
        text=f"Open {url}",
        href=url,
        clickable=True,
    )
    action_space = build_action_space([element])
    snapshot = make_snapshot(url=url, excerpt=f"Current URL: {url}", action_space=action_space)
    state = judgment_state(
        task=f"open {url}",
        snapshot=snapshot,
        history=[{"outcome": f"retried /callback?auth={query_token}#{fragment_token}"}],
    )
    criteria = build_criteria(action_space)
    select_question = select_option_question(f"choose from {url}", [url])
    model_payload = str((state, criteria, select_question))

    for secret in (path_token, query_token, fragment_token):
        assert secret not in model_payload
    assert "example.test" in model_payload


def test_snapshotter_rejects_an_unregisterable_secret() -> None:
    from fakes import FakePage

    snapshotter = Snapshotter(FakePage())
    with pytest.raises(RedactionFailure):
        snapshotter.ensure_secrets(["ab"])


def test_snapshotter_built_with_secrets_redacts_them() -> None:
    from fakes import FakePage

    snapshotter = Snapshotter(FakePage(), secrets=["a-secret-token"])
    assert snapshotter.redactor.redact("value a-secret-token") == "value [REDACTED]"


async def test_run_sanitizes_urls_before_text_generation() -> None:
    from fakes import FakeExecutor, FakeSnapshotter, RecordingTextGenerator

    secret = "unknown-task-url-token-abcdef123456"
    url = f"https://example.test/search?mystery={secret}#session={secret}"
    action_space = sample_action_space()
    generator = RecordingTextGenerator()
    result = await run(
        f"type a query for {url}",
        transport=MockTransport([step_reply(action_space, "type_e5")]),
        snapshotter=FakeSnapshotter([make_snapshot(url=url, action_space=action_space)]),
        executor=FakeExecutor(),
        text_generator=generator,
        limits=RunLimits(max_steps=1, max_jev_calls=1),
    )

    assert generator.calls
    assert secret not in str(generator.calls)
    assert result.status is not RunStatus.SUCCESS


async def test_run_still_reports_a_result_with_secrets_configured() -> None:
    from fakes import FakeExecutor, FakeSnapshotter

    snapshot = make_snapshot(action_space=sample_action_space())
    transport = MockTransport([step_reply(sample_action_space(), "done")])
    result = await run(
        "find nothing",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=FakeExecutor(),
        limits=RunLimits(max_steps=1),
        secrets=["a-real-secret-value"],
    )
    assert result.status is RunStatus.UNVERIFIED
