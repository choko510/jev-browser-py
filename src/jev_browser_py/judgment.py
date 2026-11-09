"""Strict, fail-closed validation of Jev replies.

The validator is deliberately paranoid: the model's answer is a *proposal*,
so anything that does not exactly match the questions asked becomes zero
executed actions. Every check below is covered by a test, and each failure is
a stable :class:`ErrorCode`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import (
    ActionCandidate,
    ActionKind,
    ActionProposal,
    ActionSpace,
    ControlAction,
    InvalidJudgmentError,
    Judgment,
    PageSnapshot,
    SideEffectClass,
    StaleTargetError,
    Usage,
)
from .questions import SELECT_OPTION_MAX, build_criteria
from .transport import TransportReply

__all__ = [
    "MOCK_DONE_THRESHOLD",
    "POLICY_EPSILON",
    "SELECTION_GAP_TOLERANCE",
    "assert_fresh",
    "distribution_tolerance",
    "parse_proposal",
    "side_effect_of",
    "validate_select_reply",
    "validate_step_reply",
]

MOCK_DONE_THRESHOLD = 0.85
"""Upstream compatibility threshold. A stop *proposal*, never success."""

PROBABILITY_SUM_TOLERANCE = 0.05
ROUNDING_STEP = 0.005
SELECTION_GAP_TOLERANCE = 0.01
POLICY_EPSILON = 1e-9

_CONTROL_ACTIONS = frozenset(action.value for action in ControlAction)

HIGH_RISK_WORDS: Mapping[str, tuple[str, ...]] = {
    "external_write": (
        "buy",
        "purchase",
        "checkout",
        "place order",
        "pay",
        "transfer",
        "send money",
        "post",
        "publish",
        "tweet",
        "delete",
        "remove",
        "cancel subscription",
        "unsubscribe",
        "confirm booking",
        "reserve",
        "book now",
        "submit",
    ),
    "ambiguous": (
        "continue",
        "proceed",
        "next",
        "ok",
        "yes",
        "accept",
        "agree",
        "allow",
        "share",
        "sso",
        "sign in",
        "log in",
        "register",
        "subscribe",
    ),
}


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value))


def _require_unit(value: Any, name: str) -> float:
    if not _is_finite_number(value):
        raise InvalidJudgmentError(f"{name} must be a finite number")
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise InvalidJudgmentError(f"{name} must be within [0, 1]")
    return number


def _require_exact_keys(answers: Mapping[str, Any], expected: set[str]) -> None:
    present = set(answers)
    missing = expected - present
    extra = present - expected
    if missing:
        raise InvalidJudgmentError(f"missing answers: {sorted(missing)!r}")
    if extra:
        raise InvalidJudgmentError(f"unexpected answers: {sorted(extra)!r}")


def distribution_tolerance(probabilities: Mapping[str, float]) -> float:
    """Upstream tolerance: rounding to two decimals scales with live options.

    Mirrors `jev-agent-tools` `distribution()`: each nonzero entry can be off
    by 0.005, so drift grows with option count. Clamped to [0.01, 0.05] so
    garbage still fails.
    """
    nonzero = sum(1 for value in probabilities.values() if value > 0)
    return min(PROBABILITY_SUM_TOLERANCE, max(0.01, ROUNDING_STEP * nonzero)) + 1e-9


def _validate_distribution(value: Any, criteria_keys: tuple[str, ...], *, name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise InvalidJudgmentError(f"{name}.probabilities must be an object")
    if set(value) != set(criteria_keys):
        raise InvalidJudgmentError(f"{name}.probabilities must contain exactly the criteria keys")
    cleaned: dict[str, float] = {}
    total = 0.0
    for key in criteria_keys:
        probability = value[key]
        if not _is_finite_number(probability):
            raise InvalidJudgmentError(f"{name}.probabilities[{key!r}] must be a finite number")
        number = float(probability)
        if number < 0.0 or number > 1.0:
            raise InvalidJudgmentError(f"{name}.probabilities[{key!r}] must be within [0, 1]")
        cleaned[key] = number
        total += number
    if abs(total - 1.0) > distribution_tolerance(cleaned):
        raise InvalidJudgmentError(f"{name}.probabilities must sum to approximately 1")
    return cleaned


def _validate_confidence(value: Any, *, name: str) -> float | None:
    if value is None:
        return None
    if not _is_finite_number(value):
        raise InvalidJudgmentError(f"{name}.confidence must be finite or null")
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise InvalidJudgmentError(f"{name}.confidence must be within [0, 1]")
    return number


def _action_answer(
    answers: Mapping[str, Any], *, criteria_keys: tuple[str, ...]
) -> tuple[str, dict[str, float], float | None]:
    raw = answers["action"]
    if not isinstance(raw, Mapping):
        raise InvalidJudgmentError("answer action must be an object")
    if raw.get("type") != "choice":
        raise InvalidJudgmentError("answer action must have type 'choice'")
    probabilities = _validate_distribution(raw.get("probabilities"), criteria_keys, name="action")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in probabilities:
        raise InvalidJudgmentError("answer action.choice is outside the offered criteria")
    peak = max(probabilities.values())
    if peak - probabilities[choice] > SELECTION_GAP_TOLERANCE + POLICY_EPSILON:
        raise InvalidJudgmentError("answer action.choice is not the distribution maximum")
    return choice, probabilities, _validate_confidence(raw.get("confidence"), name="action")


def _noul_answer(answers: Mapping[str, Any], name: str) -> float:
    raw = answers[name]
    if not isinstance(raw, Mapping):
        raise InvalidJudgmentError(f"answer {name} must be an object")
    if raw.get("type") != "noul":
        raise InvalidJudgmentError(f"answer {name} must have type 'noul'")
    if "noul" not in raw:
        raise InvalidJudgmentError(f"answer {name} is missing noul")
    return _require_unit(raw["noul"], f"answer {name}.noul")


def _validate_usage(input_tokens: Any, output_tokens: Any) -> None:
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidJudgmentError(f"usage.{name} must be an integer")
        if value < 0:
            raise InvalidJudgmentError(f"usage.{name} must be non-negative")


def validate_step_reply(
    reply: TransportReply,
    *,
    action_space: ActionSpace,
    snapshot: PageSnapshot,
    goal_revision: int,
) -> Judgment:
    """Validate one primary reply against the exact questions that were asked.

    Raises :class:`InvalidJudgmentError` on any deviation. Callers must treat
    that as zero executed actions for the step.
    """
    answers = reply.answers
    if not isinstance(answers, Mapping):
        raise InvalidJudgmentError("reply answers must be an object")
    _require_exact_keys(answers, {"action", "goal_done", "stuck"})
    criteria_keys = tuple(build_criteria(action_space))
    choice, probabilities, confidence = _action_answer(answers, criteria_keys=criteria_keys)
    goal_done = _noul_answer(answers, "goal_done")
    stuck = _noul_answer(answers, "stuck")
    _validate_usage(reply.input_tokens, reply.output_tokens)
    if not isinstance(reply.model, str) or not reply.model.strip():
        raise InvalidJudgmentError("model id must be a non-empty string")
    if len(reply.model) > 200:
        raise InvalidJudgmentError("model id must be at most 200 characters")
    if not isinstance(reply.provider, str) or not reply.provider.strip():
        raise InvalidJudgmentError("provider name must be a non-empty string")
    if reply.provider != "unknown" and len(reply.provider) > 100:
        raise InvalidJudgmentError("provider name must be at most 100 characters")
    return Judgment(
        action=choice,
        goal_done=goal_done,
        stuck=stuck,
        probabilities=probabilities,
        confidence=confidence,
        usage=Usage(input_tokens=reply.input_tokens, output_tokens=reply.output_tokens),
        model=reply.model,
        provider=reply.provider,
        snapshot_id=snapshot.snapshot_id,
        goal_revision=goal_revision,
    )


def validate_select_reply(reply: TransportReply, *, option_count: int) -> int:
    """Validate the second-stage native-`select` answer; returns the index."""
    if option_count <= 0 or option_count > SELECT_OPTION_MAX:
        raise InvalidJudgmentError("select option count is out of range")
    answers = reply.answers
    if not isinstance(answers, Mapping):
        raise InvalidJudgmentError("reply answers must be an object")
    _require_exact_keys(answers, {"option"})
    raw = answers["option"]
    if not isinstance(raw, Mapping):
        raise InvalidJudgmentError("answer option must be an object")
    if raw.get("type") != "choice":
        raise InvalidJudgmentError("answer option must have type 'choice'")
    criteria_keys = tuple(f"o{index}" for index in range(option_count))
    probabilities = _validate_distribution(raw.get("probabilities"), criteria_keys, name="option")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in probabilities:
        raise InvalidJudgmentError("answer option.choice is outside the offered options")
    _validate_confidence(raw.get("confidence"), name="option")
    _validate_usage(reply.input_tokens, reply.output_tokens)
    return int(choice[1:])


def side_effect_of(
    action_key: str,
    candidate: ActionCandidate | None,
    *,
    goal: str = "",
) -> SideEffectClass:
    """Classify the authorization class of one action.

    Deny-by-default reasoning: a control action is `read`, a plain fill is
    `local_input`, and anything that could leave the page (submit, or a click
    whose label reads like a transaction) is at least `ambiguous`.
    """
    if candidate is None:
        if action_key in _CONTROL_ACTIONS:
            return SideEffectClass.READ
        return SideEffectClass.AMBIGUOUS
    if candidate.kind in (ActionKind.TYPE, ActionKind.SEARCH, ActionKind.SELECT):
        return SideEffectClass.LOCAL_INPUT
    if candidate.kind is ActionKind.SUBMIT:
        if candidate.submit_via == "enter" and candidate.search_field and candidate.form_method == "get":
            return SideEffectClass.READ
        return SideEffectClass.EXTERNAL_WRITE
    lowered = f"{candidate.description} {goal}".lower()
    if any(word in lowered for word in HIGH_RISK_WORDS["external_write"]):
        return SideEffectClass.EXTERNAL_WRITE
    if any(word in lowered for word in HIGH_RISK_WORDS["ambiguous"]):
        return SideEffectClass.AMBIGUOUS
    if candidate.kind is ActionKind.CLICK:
        if candidate.is_navigation:
            return SideEffectClass.READ
        if (
            candidate.target_tag == "summary"
            or candidate.target_type in {"checkbox", "radio"}
            or candidate.target_role in {"checkbox", "radio", "switch"}
        ):
            return SideEffectClass.LOCAL_INPUT
        return SideEffectClass.AMBIGUOUS
    return SideEffectClass.AMBIGUOUS


def parse_proposal(
    judgment: Judgment,
    *,
    action_space: ActionSpace,
    goal: str = "",
) -> ActionProposal:
    """Bind a validated judgment to exactly one executable action.

    Fail-closed rules: control actions must be one of the four published
    controls, element actions must reference a candidate present in *this*
    space, and the side effect class is derived here rather than trusted from
    the model.
    """
    action = judgment.action
    if action in _CONTROL_ACTIONS:
        return ActionProposal(
            judgment=judgment,
            candidate=None,
            control=ControlAction(action),
            side_effect=side_effect_of(action, None, goal=goal),
            snapshot_id=judgment.snapshot_id,
            goal_revision=judgment.goal_revision,
        )
    candidate = action_space.by_action_key(action)
    if candidate is None:
        raise InvalidJudgmentError(f"action {action!r} is not in the current action space")
    expected_kind = action.split("_", 1)[0]
    if candidate.kind.value != expected_kind:
        raise InvalidJudgmentError("action key does not match its candidate kind")
    return ActionProposal(
        judgment=judgment,
        candidate=candidate,
        control=None,
        side_effect=side_effect_of(action, candidate, goal=goal),
        snapshot_id=judgment.snapshot_id,
        goal_revision=judgment.goal_revision,
    )


def assert_fresh(proposal: ActionProposal, snapshot: PageSnapshot) -> None:
    """Compare a proposal against the live observation before executing it.

    Freshness has two independent dimensions and both must hold:

    * **Observation identity** — the proposal is bound to this exact
      `snapshot_id`, so no observation happened between judgment and execution.
    * **Target identity** — the candidate's fingerprint still matches the
      candidate in the live action space.

    The DOM `generation` is deliberately *not* compared here: a snapshot taken
    after an action (`bump=True`) legitimately has a higher generation while
    still describing the current page. Goal revisions are compared separately
    by :func:`assert_goal_revision`, because they track the goal text rather
    than the DOM.
    """
    if proposal.snapshot_id != snapshot.snapshot_id:
        raise StaleTargetError("snapshot changed between judgment and execution; the proposal is stale")
    if proposal.candidate is not None:
        live = snapshot.action_space.by_action_key(proposal.candidate.action_key)
        if live is None:
            raise StaleTargetError("target action disappeared from the action space; the proposal is stale")
        if live.fingerprint != proposal.candidate.fingerprint:
            raise StaleTargetError("target fingerprint changed; the proposal is stale")


def assert_goal_revision(proposal: ActionProposal, goal_revision: int) -> None:
    """Reject a proposal produced for a different revision of the goal."""
    if proposal.goal_revision != goal_revision:
        raise StaleTargetError("the goal changed after this proposal was produced; the proposal is stale")
