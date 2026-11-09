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


def test_snapshotter_rejects_an_unregisterable_secret() -> None:
    from fakes import FakePage

    snapshotter = Snapshotter(FakePage())
    with pytest.raises(RedactionFailure):
        snapshotter.ensure_secrets(["ab"])


def test_snapshotter_built_with_secrets_redacts_them() -> None:
    from fakes import FakePage

    snapshotter = Snapshotter(FakePage(), secrets=["a-secret-token"])
    assert snapshotter.redactor.redact("value a-secret-token") == "value [REDACTED]"


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
