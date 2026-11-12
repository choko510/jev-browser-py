"""Provider cost reservations and global deadline regressions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from conftest import make_snapshot, step_reply
from fakes import FakeExecutor, FakeSnapshotter, acted
from jev_browser_py.contracts import ActionProposal, ActionSpace, BudgetExceededError, RunLimits, RunStatus
from jev_browser_py.runtime import run, suggest_next_action
from jev_browser_py.telemetry import run_summary
from jev_browser_py.transport import MockTransport, TransportReply
from jev_browser_py.typing import GeneratedText
from jev_browser_py.verifier import RuleBasedVerifier


async def test_run_report_uses_reserved_provider_cost(space: ActionSpace) -> None:
    transport = MockTransport(
        [step_reply(space, "done", goal_done=0.99)],
        input_tokens=100,
        output_tokens=0,
    )
    result = await run(
        'find "Annual Report 2026"',
        transport=transport,
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space, excerpt="Annual Report 2026")]),
        executor=FakeExecutor(),
        limits=RunLimits(max_cost_usd=0.25),
        verifier=RuleBasedVerifier(require_change=False),
    )

    expected = 100 * 0.042 / 1_000_000
    assert result.estimated_cost_usd == pytest.approx(expected)
    assert run_summary(result).estimated_cost_usd == pytest.approx(expected)


async def test_custom_jev_provider_without_cost_estimator_is_not_called(space: ActionSpace) -> None:
    class UnpricedTransport:
        name = "unpriced"
        calls = 0

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            del state, questions, deadline
            self.calls += 1
            return TransportReply(
                answers=step_reply(space, "scroll_down"),
                model="unpriced",
                provider=self.name,
                input_tokens=1,
                output_tokens=1,
            )

    transport = UnpricedTransport()
    executor = FakeExecutor()
    result = await run(
        "browse",
        transport=transport,  # type: ignore[arg-type]
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        limits=RunLimits(max_jev_calls=1),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.jev_calls == 0
    assert transport.calls == 0
    assert executor.action_count == 0


async def test_text_provider_without_cost_estimator_is_not_called(space: ActionSpace) -> None:
    class UnpricedGenerator:
        provider = "unpriced"
        calls = 0

        async def generate(self, *, task: str, element_description: str, url: str) -> GeneratedText:
            del task, element_description, url
            self.calls += 1
            return GeneratedText(text="sample", via=self.provider)

    generator = UnpricedGenerator()
    executor = FakeExecutor()
    result = await run(
        "type something",
        transport=MockTransport([step_reply(space, "type_e5")]),
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        text_generator=generator,  # type: ignore[arg-type]
        limits=RunLimits(max_jev_calls=1),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert generator.calls == 0
    assert executor.action_count == 0


@pytest.mark.parametrize("stage", ["policy", "verifier", "executor"])
async def test_global_deadline_bounds_policy_verifier_and_executor(stage: str, space: ActionSpace) -> None:
    class SlowPolicy:
        async def authorize(self, *, proposal: object, snapshot: object) -> object:
            del proposal, snapshot
            await asyncio.sleep(1)
            return SimpleNamespace(allowed=True, reason="")

    class SlowVerifier:
        async def verify(self, *, goal: str, before: object, after: object) -> object:
            del goal, before, after
            await asyncio.sleep(1)
            raise AssertionError("deadline should cancel verifier wait")

    class SlowExecutor:
        calls = 0

        async def execute_one(self, *, proposal: ActionProposal, snapshot: object, **kwargs: object) -> object:
            del snapshot, kwargs
            self.calls += 1
            await asyncio.sleep(1)
            return acted(proposal.action_key)

    action = "done" if stage == "verifier" else "scroll_down"
    reply = step_reply(space, action, goal_done=0.99 if stage == "verifier" else 0.01)
    executor = SlowExecutor() if stage == "executor" else FakeExecutor()
    result = await run(
        "complete one action",
        transport=MockTransport([reply]),
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        policy=SlowPolicy() if stage == "policy" else None,  # type: ignore[arg-type]
        verifier=SlowVerifier() if stage == "verifier" else RuleBasedVerifier(require_change=False),  # type: ignore[arg-type]
        limits=RunLimits(max_steps=1, max_seconds=0.02, max_jev_calls=1),
    )

    assert result.status is RunStatus.TIMEOUT
    assert result.verified is False
    if stage == "executor":
        assert executor.calls == 1
    else:
        assert getattr(executor, "action_count", 0) == 0


async def test_global_deadline_bounds_a_slow_text_generator(space: ActionSpace) -> None:
    class SlowGenerator:
        provider = "slow-generator"
        calls = 0

        def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float:
            del request
            return 0.0001

        async def generate(
            self, *, task: str, element_description: str, url: str, deadline: float | None = None
        ) -> GeneratedText:
            del task, element_description, url, deadline
            self.calls += 1
            await asyncio.sleep(1)
            return GeneratedText(text="never used", via=self.provider)

    generator = SlowGenerator()
    executor = FakeExecutor()
    result = await run(
        "fill the field",
        transport=MockTransport([step_reply(space, "type_e5")]),
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        text_generator=generator,  # type: ignore[arg-type]
        limits=RunLimits(max_seconds=0.02, max_jev_calls=1),
    )

    assert result.status is RunStatus.TIMEOUT
    assert generator.calls == 1
    assert executor.action_count == 0


async def test_suggest_next_action_fails_closed_without_cost_estimator(space: ActionSpace) -> None:
    class UnpricedTransport:
        calls = 0

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            del state, questions, deadline
            self.calls += 1
            return TransportReply(
                answers=step_reply(space, "scroll_down"),
                model="unpriced",
                provider="unpriced",
                input_tokens=1,
                output_tokens=1,
            )

    transport = UnpricedTransport()
    with pytest.raises(BudgetExceededError, match="no cost estimator"):
        await suggest_next_action(
            "browse",
            snapshot=make_snapshot(action_space=space),
            transport=transport,  # type: ignore[arg-type]
        )
    assert transport.calls == 0
