"""PYJEV-T05..T09: single-action execution, policy, and the bounded loop."""

from __future__ import annotations

import pytest

from conftest import (
    make_snapshot,
    sample_elements,
    step_reply,
)
from fakes import (
    FakeExecutor,
    FakePage,
    FakeSnapshotter,
    RecordingTextGenerator,
    acted,
    stale,
    uncertain,
)
from jev_browser_py.action_space import RawElement, build_action_space
from jev_browser_py.contracts import (
    ActionOutcomeState,
    ActionSpace,
    ErrorCode,
    RunLimits,
    RunStatus,
    SideEffectClass,
    StaleTargetError,
)
from jev_browser_py.executor import (
    ExecutorOptions,
    SingleActionExecutor,
    selector_for,
)
from jev_browser_py.judgment import parse_proposal, validate_step_reply
from jev_browser_py.policy import default_policy
from jev_browser_py.runtime import RunOptions, run
from jev_browser_py.transport import MockTransport, TransportReply
from jev_browser_py.verifier import RuleBasedVerifier


def _proposal(space: ActionSpace, action: str):  # type: ignore[no-untyped-def]
    answers = step_reply(space, action)
    reply = TransportReply(answers=answers, model="jev-mock-1", provider="mock", input_tokens=10, output_tokens=2)
    snapshot = make_snapshot(action_space=space)
    judgment = validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)
    return judgment, parse_proposal(judgment, action_space=space)


async def test_policy_denies_external_write_even_at_full_confidence(space: ActionSpace) -> None:
    elements = [
        type(sample_elements()[1])(
            attr="j1",
            tag="button",
            role="button",
            text="Place order",
            type_attr="submit",
            clickable=True,
            submit_control=True,
        )
    ]
    risky_space = build_action_space(elements)
    answers = step_reply(risky_space, "submit_e1", confidence=1.0, spread=1.0 - 1e-7)
    reply = TransportReply(answers=answers, model="jev-mock-1", provider="mock", input_tokens=1, output_tokens=1)
    snapshot = make_snapshot(action_space=risky_space)
    judgment = validate_step_reply(reply, action_space=risky_space, snapshot=snapshot, goal_revision=0)
    proposal = parse_proposal(judgment, action_space=risky_space, goal="buy something")
    decision = default_policy().decide(proposal=proposal, snapshot=snapshot)
    assert decision.allowed is False
    assert decision.side_effect is SideEffectClass.EXTERNAL_WRITE


async def test_typed_fill_is_allowed_by_default(space: ActionSpace) -> None:
    _, proposal = _proposal(space, "type_e5")
    snapshot = make_snapshot(action_space=space)
    decision = default_policy().decide(proposal=proposal, snapshot=snapshot)
    assert decision.allowed is True
    assert decision.side_effect is SideEffectClass.LOCAL_INPUT


async def test_done_is_a_gate_not_an_action(space: ActionSpace) -> None:
    _, proposal = _proposal(space, "done")
    snapshot = make_snapshot(action_space=space)
    assert default_policy().decide(proposal=proposal, snapshot=snapshot).allowed is False


async def test_executor_clicks_exactly_one_element(space: ActionSpace) -> None:
    page = FakePage()
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "click_e1")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.ACTED
    clicks = [call for call in page.calls if call[0] == "click"]
    assert len(clicks) == 1
    assert clicks[0][1] == '[data-jev-id="j1"]'


async def test_executor_refuses_a_stale_snapshot(space: ActionSpace) -> None:
    page = FakePage()
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "click_e1")
    newer = make_snapshot(action_space=space, snapshot_id="snap-2")

    with pytest.raises(StaleTargetError):
        await executor.execute_one(proposal=proposal, snapshot=newer)
    assert not [call for call in page.calls if call[0] == "click"]


async def test_executor_refuses_double_matching_locators(space: ActionSpace) -> None:
    page = FakePage()
    page.element_count = 2
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "click_e1")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.STALE
    assert outcome.error_code is ErrorCode.STALE_TARGET
    assert not [call for call in page.calls if call[0] == "click"]


async def test_executor_refuses_a_hidden_target(space: ActionSpace) -> None:
    page = FakePage()
    page.preflight_info = {**page.preflight_info, "visible": False}
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "click_e1")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.STALE
    assert not [call for call in page.calls if call[0] == "click"]


async def test_executor_never_executes_done(space: ActionSpace) -> None:
    page = FakePage()
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "done")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.BLOCKED
    assert page.calls == []


async def test_executor_classifies_risky_failure_as_uncertain(space: ActionSpace) -> None:
    elements = [
        type(sample_elements()[1])(
            attr="j1",
            tag="button",
            role="button",
            text="Save",
            type_attr="submit",
            clickable=True,
            submit_control=True,
        )
    ]
    risky_space = build_action_space(elements)
    page = FakePage()
    page.fail_with = TimeoutError("click timeout after 4000ms")
    executor = SingleActionExecutor(page)
    answers = step_reply(risky_space, "submit_e1")
    reply = TransportReply(answers=answers, model="jev-mock-1", provider="mock", input_tokens=1, output_tokens=1)
    snapshot = make_snapshot(action_space=risky_space)
    judgment = validate_step_reply(reply, action_space=risky_space, snapshot=snapshot, goal_revision=0)
    proposal = parse_proposal(judgment, action_space=risky_space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.UNCERTAIN
    assert outcome.error_code is ErrorCode.UNKNOWN_EFFECT
    assert outcome.effect_possible is True


async def test_executor_classifies_a_read_click_failure_as_failed(space: ActionSpace) -> None:
    page = FakePage()
    page.fail_with = RuntimeError("element is not attached to the DOM")
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "click_e1")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(proposal=proposal, snapshot=snapshot, current_snapshot=snapshot)

    assert outcome.state is ActionOutcomeState.FAILED
    assert outcome.error_code is ErrorCode.STALE_TARGET


async def test_executor_types_generated_text_once(space: ActionSpace) -> None:
    page = FakePage()
    page.preflight_info = {
        "tag": "input",
        "type": "text",
        "visible": True,
        "enabled": True,
        "readOnly": False,
    }
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "type_e5")
    snapshot = make_snapshot(action_space=space)

    outcome = await executor.execute_one(
        proposal=proposal,
        snapshot=snapshot,
        current_snapshot=snapshot,
        typed_text="hello@example.test",
    )

    assert outcome.state is ActionOutcomeState.ACTED
    fills = [call for call in page.calls if call[0] == "fill"]
    assert fills == [("fill", ('[data-jev-id="j5"]', "hello@example.test"))]
    assert "hello@example.test" not in outcome.detail


async def test_executor_selects_by_live_dom_index(space: ActionSpace) -> None:
    page = FakePage()
    page.preflight_info = {
        "tag": "select",
        "type": "",
        "visible": True,
        "enabled": True,
        "readOnly": False,
    }
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(space, "select_e4")
    snapshot = make_snapshot(action_space=space)

    await executor.execute_one(
        proposal=proposal,
        snapshot=snapshot,
        current_snapshot=snapshot,
        select_option_index=2,
    )

    selects = [call for call in page.calls if call[0] == "select_option"]
    assert selects == [("select_option", ('[data-jev-id="j4"]', 2))]


def test_selector_for_uses_the_candidate_stamp_not_its_index() -> None:
    """Pruning happens after stamping, so a candidate's stamp can differ."""
    space = build_action_space(
        [
            # A noise element consumes j1 but is pruned from the action space.
            type(sample_elements()[0])(attr="j1", tag="a", role="a", text="", href="/noise", clickable=True),
            type(sample_elements()[1])(attr="j2", tag="button", role="button", text="Continue", clickable=True),
        ]
    )
    candidate = space.candidates[0]
    assert candidate.id == "e1"
    assert candidate.stamp == "j2"
    assert selector_for(candidate) == '[data-jev-id="j2"]'


def test_selector_for_refuses_a_candidate_without_a_stamp() -> None:
    from jev_browser_py.contracts import ActionCandidate, ActionKind

    stamp_less = ActionCandidate(
        id="e1",
        kind=ActionKind.CLICK,
        description="button (no stamp)",
        fingerprint="f" * 32,
        action_key="click_e1",
    )
    with pytest.raises(ValueError, match="no DOM stamp"):
        selector_for(stamp_less)


def test_executor_options_reject_an_absurd_text_cap() -> None:
    with pytest.raises(ValueError, match="typed_text_max"):
        ExecutorOptions(typed_text_max=0)


async def test_loop_stops_without_acting_when_done_is_proposed(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space, excerpt="A placeholder page with nothing yet.")
    transport = MockTransport([step_reply(space, "done", goal_done=0.1)])
    snapshotter = FakeSnapshotter([snapshot])
    executor = FakeExecutor()

    result = await run(
        'find the phrase "quarterly revenue table"',
        transport=transport,
        snapshotter=snapshotter,
        executor=executor,
        verifier=RuleBasedVerifier(require_change=False),
        limits=RunLimits(max_steps=3),
    )

    assert executor.action_count == 0
    assert result.status is RunStatus.UNVERIFIED
    assert result.verified is False


async def test_loop_stops_without_acting_when_goal_done_is_high(space: ActionSpace) -> None:
    snapshot = make_snapshot(
        action_space=space,
        excerpt="The Annual Report 2026 has been published and is ready to read.",
    )
    transport = MockTransport([step_reply(space, "click_e1", goal_done=0.95)])
    executor = FakeExecutor()

    result = await run(
        'find the phrase "Annual Report 2026"',
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        verifier=RuleBasedVerifier(require_change=False),
        limits=RunLimits(max_steps=3),
    )

    assert executor.action_count == 0
    assert result.status is RunStatus.SUCCESS
    assert result.verified is True


async def test_loop_reports_stuck_without_acting(space: ActionSpace) -> None:
    snapshots = [make_snapshot(action_space=space) for _ in range(3)]
    transport = MockTransport(
        [
            step_reply(space, "scroll_down"),
            step_reply(space, "scroll_down"),
            step_reply(space, "scroll_down", stuck=0.95),
        ]
    )
    executor = FakeExecutor([acted("scroll_down") for _ in range(2)])

    result = await run(
        "browse",
        transport=transport,
        snapshotter=FakeSnapshotter(snapshots),
        executor=executor,
        limits=RunLimits(max_steps=6),
        options=RunOptions(stuck_min_step=3),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.status is RunStatus.STUCK
    assert executor.action_count == 2
    assert result.steps == 2


async def test_loop_returns_invalid_judgment_with_zero_actions(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["action"]["choice"] = "click_e999"
    transport = MockTransport([answers])
    executor = FakeExecutor()

    result = await run(
        "do something",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=3),
    )

    assert result.status is RunStatus.INVALID_JUDGMENT
    assert result.error_code is ErrorCode.INVALID_JUDGMENT
    assert executor.action_count == 0


async def test_loop_enforces_the_step_budget(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "scroll_down") for _ in range(10)])
    executor = FakeExecutor([acted("scroll_down") for _ in range(10)])

    result = await run(
        "browse forever",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=2, max_actions_per_run=10),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.status is RunStatus.UNVERIFIED
    assert executor.action_count == 2


async def test_loop_enforces_the_action_budget(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "scroll_down") for _ in range(10)])
    executor = FakeExecutor([acted("scroll_down") for _ in range(10)])

    result = await run(
        "browse",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=10, max_actions_per_run=1),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert executor.action_count == 1


async def test_loop_enforces_the_jev_call_budget(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "scroll_down") for _ in range(10)])
    executor = FakeExecutor([acted("scroll_down") for _ in range(10)])

    result = await run(
        "browse",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=10, max_jev_calls=1),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert transport.call_count == 1


async def test_over_budget_primary_judgment_cannot_report_success(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space, excerpt="Annual Report 2026")
    transport = MockTransport(
        [step_reply(space, "done", goal_done=0.99)],
        input_tokens=1_000_000,
        output_tokens=10,
    )
    executor = FakeExecutor()

    result = await run(
        'find "Annual Report 2026"',
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_cost_usd=0.01),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.verified is False
    assert executor.action_count == 0


async def test_loop_stops_unknown_effect_without_retry(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1"), step_reply(space, "click_e1")])
    executor = FakeExecutor([uncertain("click_e1")])

    result = await run(
        "click",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=5),
    )

    assert result.status is RunStatus.UNKNOWN_EFFECT
    assert result.error_code is ErrorCode.UNKNOWN_EFFECT
    assert executor.action_count == 1
    assert transport.call_count == 1


async def test_loop_stops_on_a_stale_outcome(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    executor = FakeExecutor([stale("click_e1")])

    result = await run(
        "click",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=5),
    )

    assert result.status is RunStatus.BLOCKED
    assert result.error_code is ErrorCode.STALE_TARGET


async def test_loop_never_reports_success_on_model_done_alone(space: ActionSpace) -> None:
    before = make_snapshot(action_space=space, excerpt="Nothing relevant here.", snapshot_id="a")
    after = make_snapshot(action_space=space, excerpt="still nothing", snapshot_id="b", generation=1)
    transport = MockTransport([step_reply(space, "click_e1"), step_reply(space, "done", goal_done=0.99)])
    executor = FakeExecutor([acted("click_e1")])

    result = await run(
        'find the phrase "annual report"',
        transport=transport,
        snapshotter=FakeSnapshotter([before, after]),
        executor=executor,
        limits=RunLimits(max_steps=5),
    )

    assert result.verified is False
    assert result.status is RunStatus.UNVERIFIED


async def test_loop_succeeds_only_when_the_verifier_agrees(space: ActionSpace) -> None:
    before = make_snapshot(action_space=space, excerpt="Search page", snapshot_id="a")
    after = make_snapshot(
        action_space=space,
        excerpt="Annual report 2026 is available for download.",
        snapshot_id="b",
        generation=1,
    )
    transport = MockTransport([step_reply(space, "click_e1"), step_reply(space, "done", goal_done=0.97)])
    executor = FakeExecutor([acted("click_e1")])

    result = await run(
        'find the phrase "Annual report 2026"',
        transport=transport,
        snapshotter=FakeSnapshotter([before, after]),
        executor=executor,
        limits=RunLimits(max_steps=5),
    )

    assert result.status is RunStatus.SUCCESS
    assert result.verified is True
    assert result.evidence


async def test_policy_denial_stops_with_zero_actions(space: ActionSpace) -> None:
    elements = [
        type(sample_elements()[1])(
            attr="j1",
            tag="button",
            role="button",
            text="Buy now",
            clickable=True,
        )
    ]
    risky_space = build_action_space(elements)
    snapshot = make_snapshot(action_space=risky_space)
    transport = MockTransport([step_reply(risky_space, "click_e1", spread=0.99)])
    executor = FakeExecutor()

    result = await run(
        "buy the item",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=3),
    )

    assert result.status is RunStatus.BLOCKED
    assert result.error_code is ErrorCode.POLICY_DENIED
    assert executor.action_count == 0


async def test_search_falls_back_to_a_keyword_query(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "search_e3")])
    executor = FakeExecutor([acted("search_e3")])

    await run(
        'search for "annual reports" and stop',
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=1),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert executor.typed_texts == ["annual reports"]


async def test_search_executor_does_not_submit_when_filling(space: ActionSpace) -> None:
    element = RawElement(
        attr="j1",
        tag="input",
        role="searchbox",
        text="Search",
        type_attr="search",
        typeable=True,
        search_field=True,
        enter_submittable=True,
    )
    search_space = build_action_space([element])
    page = FakePage()
    page.preflight_info = {
        "tag": "input",
        "type": "search",
        "visible": True,
        "enabled": True,
        "readOnly": False,
    }
    executor = SingleActionExecutor(page)
    _, proposal = _proposal(search_space, "search_e1")

    live_snapshot = make_snapshot(action_space=search_space)
    outcome = await executor.execute_one(
        proposal=proposal,
        snapshot=live_snapshot,
        current_snapshot=live_snapshot,
        typed_text="annual reports",
    )

    assert outcome.state is ActionOutcomeState.ACTED
    assert any(call[0] == "fill" for call in page.calls)
    assert not any(call[0] == "press" for call in page.calls)


async def test_plain_field_needs_a_generator_and_never_guesses(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "type_e5")])
    executor = FakeExecutor()

    result = await run(
        "fill in the email",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=3),
    )

    assert result.status is RunStatus.NEEDS_HUMAN
    assert executor.action_count == 0


async def test_generator_supplies_the_typed_text(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "type_e5")])
    executor = FakeExecutor([acted("type_e5")])
    generator = RecordingTextGenerator(text="hello")

    await run(
        "fill in the newsletter form",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=1),
        text_generator=generator,
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert generator.calls
    assert executor.typed_texts == ["hello"]


async def test_select_uses_a_second_judgment_call(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    criterion = {"o0": 0.05, "o1": 0.05, "o2": 0.9}
    select_answer = {
        "option": {
            "type": "choice",
            "choice": "o2",
            "confidence": 0.9,
            "probabilities": criterion,
        }
    }
    transport = MockTransport([step_reply(space, "select_e4"), select_answer])
    executor = FakeExecutor([acted("select_e4")])

    await run(
        "choose a region",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=1),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert executor.select_indices == [2]
    assert transport.call_count == 2


async def test_select_judgment_respects_remaining_jev_call_budget(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    select_answer = {
        "option": {
            "type": "choice",
            "choice": "o2",
            "confidence": 0.9,
            "probabilities": {"o0": 0.05, "o1": 0.05, "o2": 0.9},
        }
    }
    transport = MockTransport([step_reply(space, "select_e4"), select_answer])
    executor = FakeExecutor()

    result = await run(
        "choose a region",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=2, max_jev_calls=1),
    )

    assert result.status is RunStatus.BUDGET_EXCEEDED
    assert result.jev_calls == 1
    assert transport.call_count == 1
    assert executor.action_count == 0


async def test_select_judgment_usage_is_counted(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    select_answer = {
        "option": {
            "type": "choice",
            "choice": "o2",
            "confidence": 0.9,
            "probabilities": {"o0": 0.05, "o1": 0.05, "o2": 0.9},
        }
    }
    transport = MockTransport(
        [step_reply(space, "select_e4"), select_answer],
        input_tokens=7,
        output_tokens=3,
    )
    executor = FakeExecutor([acted("select_e4")])

    result = await run(
        "choose a region",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=1, max_jev_calls=2),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.jev_calls == 2
    assert result.usage.input_tokens == 14
    assert result.usage.output_tokens == 6


async def test_cancel_during_text_generation_stops_before_acting(space: ActionSpace) -> None:
    from jev_browser_py.contracts import CancelToken
    from jev_browser_py.typing import GeneratedText

    token = CancelToken()

    class CancellingGenerator:
        provider = "test"

        async def generate(self, *, task: str, element_description: str, url: str) -> GeneratedText:
            del task, element_description, url
            token.cancel("stopped while preparing text")
            return GeneratedText(text="hello", via="test")

    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "type_e5")])
    executor = FakeExecutor()
    result = await run(
        "fill in the field",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        text_generator=CancellingGenerator(),  # type: ignore[arg-type]
        cancel_token=token,
        limits=RunLimits(max_steps=2),
    )

    assert result.status is RunStatus.CANCELLED
    assert executor.action_count == 0


async def test_live_fingerprint_change_blocks_stale_candidate(space: ActionSpace) -> None:
    elements = sample_elements()
    elements[0] = type(elements[0])(
        attr=elements[0].attr,
        tag=elements[0].tag,
        role=elements[0].role,
        text=elements[0].text,
        href="https://example.test/changed",
        clickable=True,
    )
    changed_space = build_action_space(elements)
    before = make_snapshot(action_space=space, snapshot_id="before")
    live = make_snapshot(action_space=changed_space, snapshot_id="live")
    transport = MockTransport([step_reply(space, "click_e1")])
    executor = FakeExecutor()

    result = await run(
        "open the documentation",
        transport=transport,
        snapshotter=FakeSnapshotter([before, live]),
        executor=executor,
        limits=RunLimits(max_steps=2),
    )

    assert result.status is RunStatus.BLOCKED
    assert result.error_code is ErrorCode.STALE_TARGET
    assert executor.action_count == 0


async def test_select_cancel_after_secondary_judgment_stops_before_acting(space: ActionSpace) -> None:
    from jev_browser_py.contracts import CancelToken

    token = CancelToken()
    option_reply = {
        "option": {
            "type": "choice",
            "choice": "o2",
            "confidence": 0.9,
            "probabilities": {"o0": 0.05, "o1": 0.05, "o2": 0.9},
        }
    }

    class CancellingSelectTransport:
        name = "select-cancel"
        calls = 0

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            del state, deadline
            self.calls += 1
            if self.calls == 1:
                return TransportReply(
                    answers=step_reply(space, "select_e4"),
                    model="test",
                    provider="test",
                    input_tokens=1,
                    output_tokens=1,
                )
            token.cancel("cancelled during select choice")
            return TransportReply(
                answers=option_reply,
                model="test",
                provider="test",
                input_tokens=2,
                output_tokens=1,
            )

    transport = CancellingSelectTransport()
    executor = FakeExecutor()
    result = await run(
        "choose a region",
        transport=transport,
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        limits=RunLimits(max_jev_calls=2),
        cancel_token=token,
    )

    assert result.status is RunStatus.CANCELLED
    assert result.jev_calls == 2
    assert executor.action_count == 0


async def test_select_deadline_is_not_reset_for_secondary_judgment(space: ActionSpace, monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_browser_py.contracts import CancelToken

    expired = False

    def clock() -> float:
        return 100.0 if expired else 0.0

    monkeypatch.setattr("jev_browser_py.runtime._clock", clock)
    option_reply = {
        "option": {
            "type": "choice",
            "choice": "o2",
            "confidence": 0.9,
            "probabilities": {"o0": 0.05, "o1": 0.05, "o2": 0.9},
        }
    }

    class LateSelectTransport:
        name = "late-select"
        calls = 0

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            nonlocal expired
            del state, deadline
            self.calls += 1
            if self.calls == 1:
                return TransportReply(
                    answers=step_reply(space, "select_e4"),
                    model="test",
                    provider="test",
                    input_tokens=1,
                    output_tokens=1,
                )
            expired = True
            return TransportReply(
                answers=option_reply,
                model="test",
                provider="test",
                input_tokens=2,
                output_tokens=1,
            )

    transport = LateSelectTransport()
    executor = FakeExecutor()
    result = await run(
        "choose a region",
        transport=transport,
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        limits=RunLimits(max_seconds=10, max_jev_calls=2),
    )

    assert result.status is RunStatus.TIMEOUT
    assert result.jev_calls == 2
    assert executor.action_count == 0


async def test_deadline_after_text_generation_stops_before_acting(space: ActionSpace, monkeypatch: pytest.MonkeyPatch) -> None:
    from jev_browser_py.typing import GeneratedText

    expired = False

    def clock() -> float:
        return 100.0 if expired else 0.0

    monkeypatch.setattr("jev_browser_py.runtime._clock", clock)

    class LateGenerator:
        provider = "test"

        async def generate(self, *, task: str, element_description: str, url: str) -> GeneratedText:
            nonlocal expired
            del task, element_description, url
            expired = True
            return GeneratedText(text="hello", via="test")

    class ImmediateTransport:
        name = "immediate"

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            del state, questions, deadline
            return TransportReply(
                answers=step_reply(space, "type_e5"),
                model="test",
                provider="test",
                input_tokens=1,
                output_tokens=1,
            )

    executor = FakeExecutor()
    result = await run(
        "fill the field",
        transport=ImmediateTransport(),  # type: ignore[arg-type]
        snapshotter=FakeSnapshotter([make_snapshot(action_space=space)]),
        executor=executor,
        text_generator=LateGenerator(),  # type: ignore[arg-type]
        limits=RunLimits(max_seconds=10),
    )

    assert result.status is RunStatus.TIMEOUT
    assert executor.action_count == 0


async def test_concurrent_runs_on_one_page_share_the_run_lock(space: ActionSpace) -> None:
    import asyncio

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    calls = 0

    class SerialTransport:
        name = "serial"

        async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
            nonlocal calls
            del state, deadline
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
            return TransportReply(
                answers=step_reply(space, "done", goal_done=0.99),
                model="test",
                provider="test",
                input_tokens=1,
                output_tokens=1,
            )

    page = FakePage()
    first_executor = FakeExecutor()
    first_executor.page = page
    second_executor = FakeExecutor()
    second_executor.page = page
    snapshot = make_snapshot(excerpt="Annual Report 2026", action_space=space)
    transport = SerialTransport()
    verifier = RuleBasedVerifier(require_change=False)
    first = asyncio.create_task(
        run(
            'find "Annual Report 2026"',
            transport=transport,
            snapshotter=FakeSnapshotter([snapshot]),
            executor=first_executor,
            verifier=verifier,
        )
    )
    await first_entered.wait()
    second = asyncio.create_task(
        run(
            'find "Annual Report 2026"',
            transport=transport,
            snapshotter=FakeSnapshotter([snapshot]),
            executor=second_executor,
            verifier=verifier,
        )
    )
    await asyncio.sleep(0)  # Yield one event-loop turn so the second run reaches the lock.
    assert second_entered.is_set() is False
    release_first.set()
    results = await asyncio.gather(first, second)

    assert second_entered.is_set() is True
    assert all(result.status is RunStatus.SUCCESS for result in results)


async def test_deadline_stops_before_acting(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")], delay_seconds=0.05)
    executor = FakeExecutor()

    result = await run(
        "click",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=3, max_seconds=0.001),
    )

    assert result.status is RunStatus.TIMEOUT
    assert executor.action_count == 0


async def test_cancel_stops_before_observing(space: ActionSpace) -> None:
    from jev_browser_py.contracts import CancelToken

    token = CancelToken()
    token.cancel("user cancelled")
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])

    result = await run(
        "click",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=FakeExecutor(),
        limits=RunLimits(max_steps=3),
        cancel_token=token,
    )

    assert result.status is RunStatus.CANCELLED
    assert transport.call_count == 0


async def test_transport_failure_is_a_terminal_error(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([])
    executor = FakeExecutor()

    result = await run(
        "click",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=3),
    )

    assert result.status is RunStatus.ERROR
    assert result.error_code is ErrorCode.TRANSPORT_FAILED
    assert executor.action_count == 0


async def test_trace_is_redacted_and_bounded(space: ActionSpace) -> None:
    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    executor = FakeExecutor([acted("click_e1")])

    result = await run(
        "click the docs link",
        transport=transport,
        snapshotter=FakeSnapshotter([snapshot]),
        executor=executor,
        limits=RunLimits(max_steps=2),
        verifier=RuleBasedVerifier(require_change=False),
    )

    assert result.trace
    assert all(step.proposed_action for step in result.trace)
    assert result.steps == 1
