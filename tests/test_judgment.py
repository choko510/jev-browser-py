"""PYJEV-T01: the SDK/mock contract, and PYJEV-T02: invalid reply handling.

All tests here are fully offline: no API key, no network, no browser.
"""

from __future__ import annotations

import math

import pytest

from conftest import action_answer, make_snapshot, sample_action_space, step_reply
from jev_browser_py.contracts import (
    ActionKind,
    ActionSpace,
    ControlAction,
    ErrorCode,
    InvalidJudgmentError,
    StaleTargetError,
    TransportError,
)
from jev_browser_py.judgment import (
    assert_fresh,
    parse_proposal,
    validate_select_reply,
    validate_step_reply,
)
from jev_browser_py.questions import build_criteria, select_option_question, step_questions
from jev_browser_py.transport import MockTransport, TransportReply, as_transport_reply


def test_step_questions_ask_three_typed_judgments_over_one_state() -> None:
    space = sample_action_space()
    criteria = build_criteria(space)
    questions = step_questions(criteria)

    assert set(questions) == {"action", "goal_done", "stuck"}
    assert questions["action"].type == "choice"
    assert questions["goal_done"].type == "noul"
    assert questions["stuck"].type == "noul"
    assert set(questions["action"].criteria) == set(criteria)
    assert {"scroll_down", "scroll_up", "back", "done"} <= set(criteria)


def test_criteria_keys_are_exactly_the_executable_action_keys(space: ActionSpace) -> None:
    keys = set(build_criteria(space))
    action_keys = {candidate.action_key for candidate in space.candidates}
    assert action_keys <= keys
    assert keys - action_keys == {
        ControlAction.SCROLL_DOWN.value,
        ControlAction.SCROLL_UP.value,
        ControlAction.BACK.value,
        ControlAction.DONE.value,
    }


async def test_mock_transport_returns_usage_and_model(space: ActionSpace) -> None:
    import time

    transport = MockTransport([step_reply(space, "click_e1")], input_tokens=321, output_tokens=12)
    reply = await transport.judge(
        state={"task": "x"},
        questions=step_questions(build_criteria(space)),
        deadline=time.monotonic() + 5,
    )
    assert reply.input_tokens == 321
    assert reply.output_tokens == 12
    assert reply.model == "jev-mock-1"
    assert transport.call_count == 1


async def test_valid_reply_becomes_a_bound_proposal(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    reply = await transport.judge(
        state={},
        questions=step_questions(build_criteria(space)),
        deadline=time.monotonic() + 5,
    )
    judgment = validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=snapshot.generation)
    proposal = parse_proposal(judgment, action_space=space)

    assert judgment.action == "click_e1"
    assert judgment.snapshot_id == snapshot.snapshot_id
    assert proposal.candidate is not None
    assert proposal.candidate.kind is ActionKind.CLICK
    assert not proposal.is_done()


async def test_answer_id_mismatch_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    del answers["stuck"]
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="missing answers"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_unexpected_answer_id_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["extra"] = {"type": "noul", "noul": 0.5}
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="unexpected answers"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_unknown_action_id_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["action"]["choice"] = "click_e999"
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="outside the offered criteria"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_nan_probability_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["action"]["probabilities"]["click_e1"] = math.nan
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="finite"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_infinite_noul_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["goal_done"]["noul"] = math.inf
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="finite"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_probability_out_of_range_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["action"]["probabilities"]["click_e1"] = 1.4
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match=r"within \[0, 1\]"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_distribution_must_have_exactly_the_criteria_keys(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    del answers["action"]["probabilities"]["back"]
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="exactly the criteria keys"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_distribution_sum_drift_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    probabilities = answers["action"]["probabilities"]
    for key in probabilities:
        probabilities[key] = probabilities[key] * 0.5
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="sum to approximately 1"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_choice_must_be_the_distribution_maximum(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1", spread=0.9)
    probabilities = answers["action"]["probabilities"]
    # Move almost all mass to click_e2 while still summing to 1, leaving
    # click_e1 well below the peak.
    old_e1 = probabilities["click_e1"]
    probabilities["click_e1"] = 0.0
    probabilities["click_e2"] += old_e1
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="not the distribution maximum"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_wrong_answer_type_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    answers = step_reply(space, "click_e1")
    answers["action"]["type"] = "noul"
    transport = MockTransport([answers])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)

    with pytest.raises(InvalidJudgmentError, match="type 'choice'"):
        validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_invalid_usage_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)
    bad = TransportReply(
        answers=reply.answers,
        model="jev-mock-1",
        provider="mock",
        input_tokens=-1,
        output_tokens=0,
    )
    with pytest.raises(InvalidJudgmentError, match="non-negative"):
        validate_step_reply(bad, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_empty_model_id_fails_closed(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)
    bad = TransportReply(
        answers=reply.answers,
        model="   ",
        provider="mock",
        input_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(InvalidJudgmentError, match="model id"):
        validate_step_reply(bad, action_space=space, snapshot=snapshot, goal_revision=0)


async def test_script_exhaustion_is_a_transport_failure(space: ActionSpace) -> None:
    import time

    transport = MockTransport([])
    with pytest.raises(TransportError, match="script exhausted"):
        await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)


async def test_expired_deadline_is_a_transport_failure(space: ActionSpace) -> None:
    transport = MockTransport([step_reply(space, "click_e1")])
    with pytest.raises(TransportError, match="deadline expired"):
        await transport.judge(state={}, questions={}, deadline=0.0)


async def test_malformed_reply_object_is_a_transport_failure() -> None:
    with pytest.raises(TransportError, match="answers object"):
        as_transport_reply({"model": "m"}, provider="fixture")
    with pytest.raises(TransportError, match="model id"):
        as_transport_reply({"answers": {}, "model": "", "usage": {}}, provider="fixture")
    with pytest.raises(TransportError, match="input_tokens"):
        as_transport_reply({"answers": {}, "model": "m", "usage": {"input_tokens": -3}}, provider="fixture")


async def test_stale_snapshot_is_rejected_before_execution(space: ActionSpace) -> None:
    import time

    snapshot = make_snapshot(action_space=space, snapshot_id="snap-old", generation=1)
    transport = MockTransport([step_reply(space, "click_e1")])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)
    judgment = validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=snapshot.generation)
    proposal = parse_proposal(judgment, action_space=space)
    newer = make_snapshot(action_space=space, snapshot_id="snap-new", generation=2)

    with pytest.raises(StaleTargetError) as caught:
        assert_fresh(proposal, newer)
    assert caught.value.code is ErrorCode.STALE_TARGET


def test_a_higher_generation_with_the_same_snapshot_id_is_not_stale(space: ActionSpace) -> None:
    """A post-action re-observation keeps identity; only a new snapshot is stale."""
    from jev_browser_py.contracts import Judgment, Usage

    snapshot = make_snapshot(action_space=space, snapshot_id="snap-1", generation=0)
    judgment = Judgment(
        action="click_e1",
        goal_done=0.0,
        stuck=0.0,
        probabilities={"click_e1": 1.0},
        usage=Usage(),
        model="m",
        provider="fixture",
        snapshot_id=snapshot.snapshot_id,
        goal_revision=0,
    )
    proposal = parse_proposal(judgment, action_space=space)
    assert_fresh(proposal, snapshot.model_copy(update={"generation": 3}))


def test_a_changed_goal_revision_is_rejected(space: ActionSpace) -> None:
    from jev_browser_py.contracts import Judgment, Usage
    from jev_browser_py.judgment import assert_goal_revision

    judgment = Judgment(
        action="click_e1",
        goal_done=0.0,
        stuck=0.0,
        probabilities={"click_e1": 1.0},
        usage=Usage(),
        model="m",
        provider="fixture",
        snapshot_id="snap-1",
        goal_revision=1,
    )
    proposal = parse_proposal(judgment, action_space=space)
    with pytest.raises(StaleTargetError, match="goal changed"):
        assert_goal_revision(proposal, 2)


async def test_fingerprint_change_is_rejected_before_execution(space: ActionSpace) -> None:
    import time

    from conftest import sample_elements
    from jev_browser_py.action_space import build_action_space

    snapshot = make_snapshot(action_space=space)
    transport = MockTransport([step_reply(space, "click_e1")])
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)
    judgment = validate_step_reply(reply, action_space=space, snapshot=snapshot, goal_revision=snapshot.generation)
    proposal = parse_proposal(judgment, action_space=space)

    elements = sample_elements()
    elements[0] = type(elements[0])(
        attr="j1",
        tag="a",
        role="a",
        text="Documentation (moved)",
        href="https://example.test/docs",
        clickable=True,
    )
    changed = build_action_space(elements)
    newer = make_snapshot(action_space=changed, snapshot_id=snapshot.snapshot_id, generation=snapshot.generation)

    with pytest.raises(StaleTargetError, match="fingerprint"):
        assert_fresh(proposal, newer)


async def test_select_reply_validation_picks_an_index(space: ActionSpace) -> None:
    import time

    question = select_option_question("Region", ["EMEA", "APAC", "Americas"])
    assert set(question.criteria) == {"o0", "o1", "o2"}
    answer = action_answer(question.criteria, "o1", spread=0.6)
    transport = MockTransport([{"option": answer}])
    reply = await transport.judge(state={}, questions={"option": question}, deadline=time.monotonic() + 5)

    assert validate_select_reply(reply, option_count=3) == 1


async def test_select_reply_out_of_range_fails_closed() -> None:
    import time

    question = select_option_question("Region", ["EMEA", "APAC"])
    answer = action_answer(question.criteria, "o1", spread=0.9)
    transport = MockTransport([{"option": answer}])
    reply = await transport.judge(state={}, questions={"option": question}, deadline=time.monotonic() + 5)

    # The reply carries o0/o1 but the live select now offers a single option,
    # so the criteria set itself no longer matches and the answer is rejected.
    with pytest.raises(InvalidJudgmentError, match="exactly the criteria keys"):
        validate_select_reply(reply, option_count=1)


async def test_select_question_rejects_too_many_options() -> None:
    with pytest.raises(ValueError, match="too many options"):
        select_option_question("Region", [f"o{index}" for index in range(201)])


def test_control_actions_are_not_element_proposals(space: ActionSpace) -> None:
    from jev_browser_py.contracts import Judgment, Usage

    judgment = Judgment(
        action="scroll_down",
        goal_done=0.0,
        stuck=0.0,
        probabilities={"scroll_down": 1.0},
        usage=Usage(),
        model="m",
        provider="fixture",
        snapshot_id="snap-1",
        goal_revision=0,
    )
    proposal = parse_proposal(judgment, action_space=space)
    assert proposal.candidate is None
    assert proposal.control is ControlAction.SCROLL_DOWN


def test_unknown_element_action_is_rejected_from_a_bare_judgment(space: ActionSpace) -> None:
    from jev_browser_py.contracts import Judgment, Usage

    judgment = Judgment(
        action="click_e42",
        goal_done=0.0,
        stuck=0.0,
        probabilities={"click_e1": 1.0},
        usage=Usage(),
        model="m",
        provider="fixture",
        snapshot_id="snap-1",
        goal_revision=0,
    )
    with pytest.raises(InvalidJudgmentError, match="not in the current action space"):
        parse_proposal(judgment, action_space=space)


def test_empty_action_space_still_offers_controls() -> None:
    space = ActionSpace(candidates=())
    criteria = build_criteria(space)
    assert set(criteria) == {"scroll_down", "scroll_up", "back", "done"}


def test_select_question_rejects_empty_options() -> None:
    with pytest.raises(ValueError, match="at least one option"):
        select_option_question("Region", [])
