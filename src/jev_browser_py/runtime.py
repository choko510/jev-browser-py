"""The bounded Observe → Judge → Act loop.

Control flow lives here; Jev owns only the judgments. The loop is deliberately
linear and boring so every gate is visible in one place:

1. Check cancellation, deadline, and remaining budget.
2. Observe and redact the page (bumping the DOM generation).
3. Ask the three typed questions (one call, one state).
4. Validate strictly; a bad reply means zero actions and a terminal status.
5. Stop gates run *before* any action: `done`, high `goal_done`, `stuck`.
6. Authorize through the policy; a high model confidence is not authorization.
7. Re-check freshness under the action lock, execute exactly one action, and
   re-observe.
8. An unknown effect stops the run without retrying.

Success requires the independent verifier. Nothing else sets `verified=True`.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    ActionKind,
    ActionOutcome,
    ActionOutcomeState,
    ActionProposal,
    BudgetExceededError,
    CancelledError,
    CancelToken,
    ErrorCode,
    EvidenceRef,
    InvalidJudgmentError,
    Judgment,
    NetworkGuardError,
    PageSnapshot,
    PolicyDeniedError,
    RunLimits,
    RunResult,
    RunStatus,
    SideEffectClass,
    StaleTargetError,
    StepTrace,
    TransportError,
    Usage,
)
from .judgment import (
    MOCK_DONE_THRESHOLD,
    parse_proposal,
    validate_select_reply,
    validate_step_reply,
)
from .policy import ActionPolicy, default_policy
from .questions import build_criteria, judgment_state, select_option_question, step_questions
from .redaction import Redactor, redact_url_for_model, redact_urls_in_text
from .snapshot import Snapshotter
from .transport import JevTransport, TransportReply
from .typing import TextGenerationError, TextGenerator, is_sensitive_field
from .verifier import RuleBasedVerifier, VerificationError, Verifier

__all__ = [
    "ExecutorProtocol",
    "RunContext",
    "RunOptions",
    "SnapshotProvider",
    "run",
    "suggest_next_action",
]

DONE_THRESHOLD = MOCK_DONE_THRESHOLD
STUCK_THRESHOLD = MOCK_DONE_THRESHOLD
STUCK_MIN_STEP = 3
HISTORY_LIMIT = 8


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Behavioural options that are not budget limits."""

    allow_typed_input: bool = True
    allow_select: bool = True
    allow_scroll: bool = True
    allow_back: bool = True
    max_history: int = HISTORY_LIMIT
    done_threshold: float = DONE_THRESHOLD
    stuck_threshold: float = STUCK_THRESHOLD
    stuck_min_step: int = STUCK_MIN_STEP
    excerpt_chars: int = 1_500
    element_cap: int = 240


class SnapshotProvider:
    """Protocol-ish base for anything that can observe the page."""

    async def observe(self, *, bump: bool = False) -> PageSnapshot:  # pragma: no cover
        raise NotImplementedError

    def bump_generation(self) -> int:  # pragma: no cover
        raise NotImplementedError


class ExecutorProtocol:
    """Protocol-ish base for the one-action executor."""

    async def execute_one(
        self,
        *,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        current_snapshot: PageSnapshot | None = None,
        typed_text: str | None = None,
        select_option_index: int | None = None,
    ) -> ActionOutcome:  # pragma: no cover
        raise NotImplementedError


@dataclass
class RunContext:
    """Everything one run needs, injected so the loop stays testable."""

    goal: str
    transport: JevTransport
    snapshotter: Snapshotter
    executor: Any
    policy: ActionPolicy
    verifier: Verifier
    cost_budget: _CostBudget
    limits: RunLimits = field(default_factory=RunLimits)
    options: RunOptions = field(default_factory=RunOptions)
    text_generator: TextGenerator | None = None
    redactor: Redactor | None = None
    cancel_token: CancelToken | None = None
    goal_revision: int = 0


def _clock() -> float:
    return time.monotonic()


class _CostBudgetError(RuntimeError):
    pass


@dataclass
class _CostBudget:
    limit_usd: float
    reserved_usd: float = 0.0

    def reserve(self, provider: object, request: Mapping[str, object]) -> float:
        estimator = getattr(provider, "estimate_max_cost_usd", None)
        name = str(getattr(provider, "name", getattr(provider, "provider", type(provider).__name__)))
        if not callable(estimator):
            raise _CostBudgetError(f"provider {name} has no cost estimator; request was not sent")
        try:
            estimate = estimator(request)
        except Exception as error:
            raise _CostBudgetError(f"provider {name} cost estimation failed") from error
        if estimate is None:
            raise _CostBudgetError(f"provider {name} did not provide a cost estimate; request was not sent")
        if isinstance(estimate, bool) or not isinstance(estimate, int | float) or not math.isfinite(float(estimate)):
            raise _CostBudgetError(f"provider {name} returned an invalid cost estimate")
        estimate = float(estimate)
        if estimate < 0:
            raise _CostBudgetError(f"provider {name} returned a negative cost estimate")
        if self.reserved_usd + estimate > self.limit_usd:
            raise _CostBudgetError(f"cost budget of ${self.limit_usd:.4f} would be exceeded")
        self.reserved_usd += estimate
        return estimate


async def _await_before_deadline[T](awaitable: Awaitable[T], deadline: float) -> T:
    """Return by the run deadline even when a provider ignores its deadline argument."""
    remaining = deadline - _clock()
    if remaining <= 0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError("run deadline expired")
    task = asyncio.ensure_future(awaitable)
    completed, _ = await asyncio.wait((task,), timeout=remaining)
    if task not in completed:
        task.cancel()
        task.add_done_callback(_consume_cancelled_task)
        raise TimeoutError("run deadline expired")
    return task.result()


def _consume_cancelled_task(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


async def _judge(
    *,
    context: RunContext,
    snapshot: PageSnapshot,
    history: Sequence[Mapping[str, Any]],
    deadline: float,
) -> tuple[Judgment, ActionProposal]:
    """One judgment call: ask, validate, bind. Any failure is fail-closed."""
    state = judgment_state(task=context.goal, snapshot=snapshot, history=history)
    criteria = build_criteria(snapshot.action_space)
    questions = step_questions(criteria)
    context.cost_budget.reserve(context.transport, {"state": state, "questions": questions})
    reply = await _await_before_deadline(
        context.transport.judge(state=state, questions=questions, deadline=deadline), deadline
    )
    judgment = validate_step_reply(
        reply,
        action_space=snapshot.action_space,
        snapshot=snapshot,
        goal_revision=context.goal_revision,
    )
    proposal = parse_proposal(judgment, action_space=snapshot.action_space, goal=context.goal)
    return judgment, proposal


async def _judge_select(
    *,
    context: RunContext,
    snapshot: PageSnapshot,
    proposal: ActionProposal,
    deadline: float,
) -> TransportReply:
    """Fetch the second-stage option judgment for a native `<select>`."""
    candidate = proposal.candidate
    if candidate is None or not candidate.options:
        raise InvalidJudgmentError("select action has no options to choose from")
    question = select_option_question(candidate.description, list(candidate.options))
    state = {
        "task": redact_urls_in_text(context.goal),
        "page": {
            "url": redact_url_for_model(snapshot.url),
            "title": redact_urls_in_text(snapshot.title),
        },
        "dropdown": redact_urls_in_text(candidate.description),
        "options": [redact_urls_in_text(option) for option in candidate.options],
    }
    questions = {"option": question}
    context.cost_budget.reserve(context.transport, {"state": state, "questions": questions})
    return await _await_before_deadline(
        context.transport.judge(state=state, questions=questions, deadline=deadline), deadline
    )


async def run(
    goal: str,
    *,
    transport: JevTransport,
    snapshotter: Snapshotter,
    executor: Any,
    policy: ActionPolicy | None = None,
    verifier: Verifier | None = None,
    limits: RunLimits | None = None,
    options: RunOptions | None = None,
    text_generator: TextGenerator | None = None,
    secrets: Sequence[str] = (),
    cancel_token: CancelToken | None = None,
) -> RunResult:
    """Run one serialized goal on the Page supplied by the snapshotter/executor.

    The caller owns the browser. This function never launches or closes a
    browser; it automatically holds that Page's shared action lock for the run.
    Do not wrap `run()` in `BrowserSession.action_lock` a second time.
    """
    snapshot_page = getattr(snapshotter, "page", None)
    executor_page = getattr(executor, "page", None)
    if snapshot_page is not None and executor_page is not None and snapshot_page is not executor_page:
        raise ValueError("snapshotter and executor must target the same Page")
    page = snapshot_page if snapshot_page is not None else executor_page
    if page is None:
        return await _run_unlocked(
            goal,
            transport=transport,
            snapshotter=snapshotter,
            executor=executor,
            policy=policy,
            verifier=verifier,
            limits=limits,
            options=options,
            text_generator=text_generator,
            secrets=secrets,
            cancel_token=cancel_token,
        )
    from .session import _page_action_lock

    async with _page_action_lock(page):
        return await _run_unlocked(
            goal,
            transport=transport,
            snapshotter=snapshotter,
            executor=executor,
            policy=policy,
            verifier=verifier,
            limits=limits,
            options=options,
            text_generator=text_generator,
            secrets=secrets,
            cancel_token=cancel_token,
        )


async def _run_unlocked(
    goal: str,
    *,
    transport: JevTransport,
    snapshotter: Snapshotter,
    executor: Any,
    policy: ActionPolicy | None = None,
    verifier: Verifier | None = None,
    limits: RunLimits | None = None,
    options: RunOptions | None = None,
    text_generator: TextGenerator | None = None,
    secrets: Sequence[str] = (),
    cancel_token: CancelToken | None = None,
) -> RunResult:
    """Execute one goal; the public wrapper owns the Page action lock."""
    resolved_limits = limits or RunLimits()
    resolved_options = options or RunOptions()
    configured_secrets = list(dict.fromkeys(secrets))
    if configured_secrets:
        # The redactor must be installed on the *snapshotter*, which is the
        # component that reads page text. Building one and never attaching it
        # would look like protection while leaking everything.
        snapshotter.ensure_secrets(configured_secrets)
    redactor = snapshotter.redactor
    cost_budget = _CostBudget(resolved_limits.max_cost_usd)
    context = RunContext(
        goal=goal,
        transport=transport,
        snapshotter=snapshotter,
        executor=executor,
        policy=policy or default_policy(),
        verifier=verifier or RuleBasedVerifier(),
        cost_budget=cost_budget,
        limits=resolved_limits,
        options=resolved_options,
        text_generator=text_generator,
        redactor=redactor,
        cancel_token=cancel_token,
        goal_revision=0,
    )
    started = _clock()
    deadline = started + resolved_limits.max_seconds
    usage = Usage()
    jev_calls = 0
    actions_taken = 0
    trace: list[StepTrace] = []
    history: list[dict[str, Any]] = []
    last_snapshot: PageSnapshot | None = None
    first_snapshot: PageSnapshot | None = None

    def _result(
        status: RunStatus,
        reason: str,
        *,
        error_code: ErrorCode | None = None,
        verified: bool = False,
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> RunResult:
        return RunResult(
            status=status,
            reason=reason,
            verified=verified,
            steps=actions_taken,
            usage=usage,
            estimated_cost_usd=cost_budget.reserved_usd,
            jev_calls=jev_calls,
            elapsed_seconds=max(0.0, _clock() - started),
            final_url=redact_url_for_model(last_snapshot.url) if last_snapshot else "",
            final_snapshot_id=last_snapshot.snapshot_id if last_snapshot else None,
            evidence=evidence,
            trace=tuple(trace),
            error_code=error_code,
        )

    page = getattr(snapshotter, "page", None)
    if page is None:
        page = getattr(executor, "page", None)
    if page is not None and getattr(page, "context", None) is not None:
        from .session import _network_policy_for_page

        if _network_policy_for_page(page) is None:
            return _result(
                RunStatus.BLOCKED,
                "a guarded BrowserSession is required for a Playwright Page",
                error_code=ErrorCode.NETWORK_BLOCKED,
            )

    def _stop_after_wait() -> RunResult | None:
        if context.cancel_token is not None and context.cancel_token.cancelled:
            return _result(RunStatus.CANCELLED, context.cancel_token.reason, error_code=ErrorCode.CANCELLED)
        if _clock() >= deadline:
            return _result(
                RunStatus.TIMEOUT,
                "the run deadline expired before the next action",
                error_code=ErrorCode.TIMEOUT,
            )
        return None

    step_number = 0
    while True:
        step_number += 1
        # ── Gates before any work ───────────────────────────────────────────
        if context.cancel_token is not None and context.cancel_token.cancelled:
            return _result(RunStatus.CANCELLED, context.cancel_token.reason, error_code=ErrorCode.CANCELLED)
        now = _clock()
        if now >= deadline:
            return _result(
                RunStatus.TIMEOUT,
                "the run deadline expired",
                error_code=ErrorCode.TIMEOUT,
            )
        if step_number > resolved_limits.max_steps:
            return _result(
                RunStatus.BUDGET_EXCEEDED,
                f"step budget of {resolved_limits.max_steps} exhausted",
                error_code=ErrorCode.BUDGET_EXCEEDED,
            )
        if jev_calls >= resolved_limits.max_jev_calls:
            return _result(
                RunStatus.BUDGET_EXCEEDED,
                f"Jev call budget of {resolved_limits.max_jev_calls} exhausted",
                error_code=ErrorCode.BUDGET_EXCEEDED,
            )
        if actions_taken >= resolved_limits.max_actions_per_run:
            return _result(
                RunStatus.BUDGET_EXCEEDED,
                f"action budget of {resolved_limits.max_actions_per_run} exhausted",
                error_code=ErrorCode.BUDGET_EXCEEDED,
            )

        # ── Observe ─────────────────────────────────────────────────────────
        try:
            snapshot = await _await_before_deadline(snapshotter.observe(), deadline)
        except TimeoutError:
            return _result(
                RunStatus.TIMEOUT,
                "snapshot observation exceeded the run deadline",
                error_code=ErrorCode.TIMEOUT,
            )
        except NetworkGuardError as error:
            return _result(RunStatus.BLOCKED, error.message, error_code=error.code)
        except CancelledError:
            return _result(RunStatus.CANCELLED, "cancelled while observing", error_code=ErrorCode.CANCELLED)
        except Exception as error:  # noqa: BLE001 - observation failures are terminal
            return _result(
                RunStatus.ERROR,
                f"the page could not be observed: {type(error).__name__}",
                error_code=_error_code_of(error),
            )
        last_snapshot = snapshot
        if first_snapshot is None:
            first_snapshot = snapshot
        if snapshot.redaction.value == "failed":
            return _result(
                RunStatus.BLOCKED,
                "redaction failed; refusing to send page text to the model",
                error_code=ErrorCode.REDACTION_FAILED,
            )

        # ── Judge ───────────────────────────────────────────────────────────
        try:
            judgment, proposal = await _judge(context=context, snapshot=snapshot, history=history, deadline=deadline)
            jev_calls += 1
            usage = Usage(
                input_tokens=usage.input_tokens + judgment.usage.input_tokens,
                output_tokens=usage.output_tokens + judgment.usage.output_tokens,
            )
        except _CostBudgetError as error:
            return _result(RunStatus.BUDGET_EXCEEDED, str(error), error_code=ErrorCode.BUDGET_EXCEEDED)
        except TransportError as error:
            return _result(RunStatus.ERROR, error.message, error_code=ErrorCode.TRANSPORT_FAILED)
        except TimeoutError:
            return _result(
                RunStatus.TIMEOUT,
                "the judgment call did not finish before the deadline",
                error_code=ErrorCode.TIMEOUT,
            )
        except InvalidJudgmentError as error:
            return _result(
                RunStatus.INVALID_JUDGMENT,
                "the model reply was invalid; no action was executed: " + error.message,
                error_code=ErrorCode.INVALID_JUDGMENT,
            )
        except CancelledError:
            return _result(RunStatus.CANCELLED, "cancelled while judging", error_code=ErrorCode.CANCELLED)

        trace.append(
            StepTrace(
                step=step_number,
                snapshot_id=snapshot.snapshot_id,
                generation=snapshot.generation,
                proposed_action=judgment.action,
                outcome="proposed",
                confidence=judgment.confidence,
                goal_done=judgment.goal_done,
                stuck=judgment.stuck,
                side_effect=proposal.side_effect,
                model=judgment.model,
            )
        )

        # ── Stop gates, before any action is considered ─────────────────────
        baseline = first_snapshot or snapshot
        if proposal.is_done():
            try:
                verified, detail, evidence = await _await_before_deadline(
                    _verify(context, goal=goal, before=baseline, after=snapshot), deadline
                )
            except TimeoutError:
                return _result(
                    RunStatus.TIMEOUT,
                    "verification exceeded the run deadline",
                    error_code=ErrorCode.TIMEOUT,
                )
            trace[-1] = trace[-1].model_copy(update={"outcome": "done proposed", "detail": detail})
            return _result(
                RunStatus.SUCCESS if verified else RunStatus.UNVERIFIED,
                "the model proposed done; " + detail,
                verified=verified,
                evidence=evidence,
            )
        if judgment.goal_done > resolved_options.done_threshold:
            try:
                verified, detail, evidence = await _await_before_deadline(
                    _verify(context, goal=goal, before=baseline, after=snapshot), deadline
                )
            except TimeoutError:
                return _result(
                    RunStatus.TIMEOUT,
                    "verification exceeded the run deadline",
                    error_code=ErrorCode.TIMEOUT,
                )
            trace[-1] = trace[-1].model_copy(update={"outcome": "goal_done proposed", "detail": detail})
            return _result(
                RunStatus.SUCCESS if verified else RunStatus.UNVERIFIED,
                f"goal_done={judgment.goal_done:.2f}; {detail}",
                verified=verified,
                evidence=evidence,
            )
        if judgment.stuck > resolved_options.stuck_threshold and step_number >= resolved_options.stuck_min_step:
            trace[-1] = trace[-1].model_copy(update={"outcome": "stuck", "detail": "the model reported no progress"})
            return _result(
                RunStatus.STUCK,
                f"stuck={judgment.stuck:.2f}; stopping without further actions",
                error_code=ErrorCode.INVALID_JUDGMENT,
            )

        # A slow judgment must not authorize an action after the deadline. The
        # transport is asked to honour the deadline, but the loop re-checks it
        # because a provider that returns late is still a late reply.
        if context.cancel_token is not None and context.cancel_token.cancelled:
            return _result(RunStatus.CANCELLED, context.cancel_token.reason, error_code=ErrorCode.CANCELLED)
        if _clock() >= deadline:
            return _result(
                RunStatus.TIMEOUT,
                "the judgment returned after the deadline; no action was executed",
                error_code=ErrorCode.TIMEOUT,
            )

        # ── Policy ──────────────────────────────────────────────────────────
        try:
            decision = await _await_before_deadline(
                context.policy.authorize(proposal=proposal, snapshot=snapshot), deadline
            )
        except TimeoutError:
            return _result(
                RunStatus.TIMEOUT,
                "policy authorization exceeded the run deadline",
                error_code=ErrorCode.TIMEOUT,
            )
        if not decision.allowed:
            trace[-1] = trace[-1].model_copy(update={"outcome": "policy_denied", "detail": decision.reason})
            return _result(
                RunStatus.BLOCKED,
                f"policy denied {proposal.action_key}: {decision.reason}",
                error_code=ErrorCode.POLICY_DENIED,
            )
        stop_result = _stop_after_wait()
        if stop_result is not None:
            return stop_result

        # Native-select option choice is a second Jev call and consumes the same
        # call, token, cost, cancellation, and wall-clock budgets as the primary
        # judgment. It must finish before the selected option can be executed.
        select_index: int | None = None
        prep_error: str | None = None
        candidate = proposal.candidate
        if candidate is not None and candidate.kind is ActionKind.SELECT:
            if not context.options.allow_select:
                prep_error = "select actions are disabled by configuration"
            elif not candidate.options:
                prep_error = "the select control offered no options"
            elif jev_calls >= resolved_limits.max_jev_calls:
                return _result(
                    RunStatus.BUDGET_EXCEEDED,
                    f"Jev call budget of {resolved_limits.max_jev_calls} exhausted before select choice",
                    error_code=ErrorCode.BUDGET_EXCEEDED,
                )
            else:
                jev_calls += 1
                try:
                    select_reply = await _judge_select(
                        context=context,
                        snapshot=snapshot,
                        proposal=proposal,
                        deadline=deadline,
                    )
                    select_index = validate_select_reply(select_reply, option_count=len(candidate.options))
                    usage = Usage(
                        input_tokens=usage.input_tokens + select_reply.input_tokens,
                        output_tokens=usage.output_tokens + select_reply.output_tokens,
                    )
                except _CostBudgetError as error:
                    jev_calls -= 1
                    return _result(RunStatus.BUDGET_EXCEEDED, str(error), error_code=ErrorCode.BUDGET_EXCEEDED)
                except TransportError as error:
                    return _result(RunStatus.ERROR, error.message, error_code=ErrorCode.TRANSPORT_FAILED)
                except TimeoutError:
                    return _result(
                        RunStatus.TIMEOUT,
                        "the select judgment exceeded the run deadline",
                        error_code=ErrorCode.TIMEOUT,
                    )
                except InvalidJudgmentError as error:
                    return _result(
                        RunStatus.INVALID_JUDGMENT,
                        "the select reply was invalid; no option was selected: " + error.message,
                        error_code=ErrorCode.INVALID_JUDGMENT,
                    )
                except CancelledError:
                    return _result(
                        RunStatus.CANCELLED,
                        "cancelled while choosing a select option",
                        error_code=ErrorCode.CANCELLED,
                    )
                stop_result = _stop_after_wait()
                if stop_result is not None:
                    return stop_result
        else:
            prep_error = None

        if candidate is not None and candidate.kind is ActionKind.SELECT and prep_error is not None:
            trace[-1] = trace[-1].model_copy(update={"outcome": "input_unavailable", "detail": prep_error})
            return _result(RunStatus.NEEDS_HUMAN, prep_error, error_code=ErrorCode.NEEDS_HUMAN)

        try:
            typed_text, select_index, prep_error = await _prepare_action_input(
                context=context,
                proposal=proposal,
                snapshot=snapshot,
                select_option_index=select_index,
                deadline=deadline,
            )
        except _CostBudgetError as error:
            return _result(RunStatus.BUDGET_EXCEEDED, str(error), error_code=ErrorCode.BUDGET_EXCEEDED)
        except TimeoutError:
            return _result(
                RunStatus.TIMEOUT,
                "input preparation exceeded the run deadline",
                error_code=ErrorCode.TIMEOUT,
            )
        if prep_error is not None:
            trace[-1] = trace[-1].model_copy(update={"outcome": "input_unavailable", "detail": prep_error})
            return _result(RunStatus.NEEDS_HUMAN, prep_error, error_code=ErrorCode.NEEDS_HUMAN)
        stop_result = _stop_after_wait()
        if stop_result is not None:
            return stop_result

        live_snapshot = snapshot
        if proposal.candidate is not None:
            try:
                live_snapshot = await _await_before_deadline(snapshotter.observe(), deadline)
            except TimeoutError:
                return _result(
                    RunStatus.TIMEOUT,
                    "live target observation exceeded the run deadline; no action was executed",
                    error_code=ErrorCode.TIMEOUT,
                )
            except Exception as error:  # noqa: BLE001 - current target must be observable to act
                trace[-1] = trace[-1].model_copy(
                    update={"outcome": "stale", "detail": "live target could not be re-observed"}
                )
                return _result(
                    RunStatus.BLOCKED,
                    "the live target could not be re-observed; no action was executed",
                    error_code=_error_code_of(error),
                )
            live_matches = [
                live
                for live in live_snapshot.action_space.candidates
                if live.stamp == proposal.candidate.stamp and live.kind is proposal.candidate.kind
            ]
            if len(live_matches) != 1 or live_matches[0].fingerprint != proposal.candidate.fingerprint:
                trace[-1] = trace[-1].model_copy(
                    update={"outcome": "stale", "detail": "the candidate fingerprint changed before execution"}
                )
                return _result(
                    RunStatus.BLOCKED,
                    "the target changed before execution; nothing was clicked",
                    error_code=ErrorCode.STALE_TARGET,
                )
        stop_result = _stop_after_wait()
        if stop_result is not None:
            return stop_result

        # ── Act ─────────────────────────────────────────────────────────────
        try:
            from .session import _authorize_unsafe_requests_for_page

            allow_unsafe_requests = decision.allowed and proposal.side_effect in (
                SideEffectClass.EXTERNAL_WRITE,
                SideEffectClass.AMBIGUOUS,
            )
            with _authorize_unsafe_requests_for_page(page, allowed=allow_unsafe_requests):
                outcome = await _await_before_deadline(
                    context.executor.execute_one(
                        proposal=proposal,
                        snapshot=snapshot,
                        current_snapshot=live_snapshot,
                        typed_text=typed_text,
                        select_option_index=select_index,
                    ),
                    deadline,
                )
        except TimeoutError:
            if proposal.side_effect in {SideEffectClass.EXTERNAL_WRITE, SideEffectClass.AMBIGUOUS}:
                return _result(
                    RunStatus.UNKNOWN_EFFECT,
                    "an authorized write action timed out; its effect is unknown and will not be retried",
                    error_code=ErrorCode.UNKNOWN_EFFECT,
                )
            return _result(RunStatus.TIMEOUT, "the action exceeded the run deadline", error_code=ErrorCode.TIMEOUT)
        except StaleTargetError as error:
            trace[-1] = trace[-1].model_copy(update={"outcome": "stale", "detail": error.message})
            return _result(
                RunStatus.BLOCKED,
                "the target changed before execution; nothing was clicked: " + error.message,
                error_code=ErrorCode.STALE_TARGET,
            )
        except PolicyDeniedError as error:
            return _result(RunStatus.BLOCKED, error.message, error_code=ErrorCode.POLICY_DENIED)
        except CancelledError:
            return _result(RunStatus.CANCELLED, "cancelled before acting", error_code=ErrorCode.CANCELLED)
        except Exception as error:  # noqa: BLE001 - an unexpected executor fault stops the run
            return _result(
                RunStatus.ERROR,
                f"the executor failed: {type(error).__name__}",
                error_code=ErrorCode.UNKNOWN_EFFECT,
            )

        actions_taken += 1
        trace[-1] = trace[-1].model_copy(
            update={
                "executed_action": proposal.action_key,
                "outcome": outcome.state.value,
                "detail": outcome.detail,
                "action_error": outcome.error_code.value if outcome.error_code else None,
            }
        )
        history.append(
            {
                "step": step_number,
                "action": proposal.action_key,
                "outcome": outcome.detail[:160],
            }
        )
        if len(history) > resolved_options.max_history:
            del history[0 : len(history) - resolved_options.max_history]

        # ── Unknown effect stops the run, with no retry ─────────────────────
        if outcome.state is ActionOutcomeState.UNCERTAIN or (
            outcome.effect_possible and outcome.state is not ActionOutcomeState.ACTED
        ):
            return _result(
                RunStatus.UNKNOWN_EFFECT,
                "an action may have reached the server and its result is unknown; not retried",
                error_code=ErrorCode.UNKNOWN_EFFECT,
            )
        if outcome.state is ActionOutcomeState.NEEDS_HUMAN:
            return _result(RunStatus.NEEDS_HUMAN, outcome.detail, error_code=ErrorCode.NEEDS_HUMAN)
        if outcome.state is ActionOutcomeState.STALE:
            return _result(RunStatus.BLOCKED, outcome.detail, error_code=ErrorCode.STALE_TARGET)
        if outcome.state is ActionOutcomeState.BLOCKED:
            return _result(RunStatus.BLOCKED, outcome.detail, error_code=outcome.error_code or ErrorCode.POLICY_DENIED)

        # ── Re-observe, then let the verifier have the last word ────────────
        try:
            after = await _await_before_deadline(snapshotter.observe(bump=True), deadline)
        except TimeoutError:
            return _result(
                RunStatus.UNKNOWN_EFFECT,
                "the run deadline expired after acting; the result could not be observed",
                error_code=ErrorCode.UNKNOWN_EFFECT,
            )
        except Exception:  # noqa: BLE001 - post-action observation is terminal
            return _result(
                RunStatus.UNKNOWN_EFFECT,
                "the page could not be re-observed after acting; the effect is unknown",
                error_code=ErrorCode.UNKNOWN_EFFECT,
            )
        last_snapshot = after
        try:
            verified, detail, evidence = await _await_before_deadline(
                _verify(context, goal=goal, before=snapshot, after=after), deadline
            )
        except TimeoutError:
            return _result(
                RunStatus.TIMEOUT,
                "verification exceeded the run deadline; the action was not declared successful",
                error_code=ErrorCode.TIMEOUT,
            )
        if verified:
            return _result(
                RunStatus.SUCCESS,
                "the independent verifier confirmed the goal: " + detail,
                verified=True,
                evidence=evidence,
            )
        if step_number >= resolved_limits.max_steps:
            return _result(
                RunStatus.UNVERIFIED,
                "the step budget was exhausted before the goal could be verified",
                error_code=ErrorCode.VERIFICATION_FAILED,
                evidence=evidence,
            )


def _error_code_of(error: Exception) -> ErrorCode:
    code = getattr(error, "code", None)
    if isinstance(code, ErrorCode):
        return code
    return ErrorCode.ERROR


async def _verify(
    context: RunContext, *, goal: str, before: PageSnapshot, after: PageSnapshot
) -> tuple[bool, str, tuple[EvidenceRef, ...]]:
    try:
        result = await context.verifier.verify(goal=goal, before=before, after=after)
    except VerificationError:
        return (False, "verification could not run", ())
    except Exception as error:  # noqa: BLE001 - verification failures are never success
        return (False, f"verification failed ({type(error).__name__})", ())
    return (result.verified, result.detail, result.evidence)


async def _prepare_action_input(
    *,
    context: RunContext,
    proposal: ActionProposal,
    snapshot: PageSnapshot,
    select_option_index: int | None = None,
    deadline: float,
) -> tuple[str | None, int | None, str | None]:
    """Prepare typed text after any budgeted select judgment has completed.

    Returns `(typed_text, select_index, error)`. An error means the run cannot
    safely proceed: a field that needs a human value must not be filled with a
    guess.
    """
    candidate = proposal.candidate
    if candidate is None:
        return (None, None, None)
    from .contracts import ActionKind

    if candidate.kind is ActionKind.SELECT:
        if not context.options.allow_select:
            return (None, None, "select actions are disabled by configuration")
        if not candidate.options:
            return (None, None, "the select control offered no options")
        if select_option_index is None:
            return (None, None, "no validated select option was supplied")
        return (None, select_option_index, None)
    if candidate.kind.value not in {"type", "search"}:
        return (None, None, None)
    if not context.options.allow_typed_input:
        return (None, None, "typed input is disabled by configuration")
    if is_sensitive_field(candidate.description):
        return (
            None,
            None,
            "this field needs a user-supplied credential; no automation value is available",
        )
    if context.text_generator is None:
        if candidate.kind.value == "search":
            from .typing import GeneratedText, heuristic_query

            query = heuristic_query(context.goal)
            if not query:
                return (None, None, "no search text could be derived from the task")
            return (GeneratedText(text=query, via="keyword-heuristic").text, None, None)
        return (
            None,
            None,
            "no text generator is configured; an ordinary field is left empty rather than guessed",
        )
    cost_request = {
        "task": redact_urls_in_text(context.goal),
        "element_description": redact_urls_in_text(candidate.description),
        "url": redact_url_for_model(snapshot.url),
    }
    context.cost_budget.reserve(context.text_generator, cost_request)
    try:
        generated = await _await_before_deadline(
            context.text_generator.generate(**cost_request, deadline=deadline), deadline
        )
    except TimeoutError:
        raise
    except TextGenerationError as error:
        if candidate.kind.value == "search":
            from .typing import heuristic_query

            query = heuristic_query(context.goal)
            if query:
                return (query, None, None)
        return (None, None, f"text generation failed: {error.message}")
    except Exception:  # noqa: BLE001 - a generator fault must not fabricate text
        return (None, None, "text generation failed")
    return (generated.text, None, None)


async def suggest_next_action(
    goal: str,
    *,
    snapshot: PageSnapshot,
    transport: JevTransport,
    goal_revision: int = 0,
    deadline: float | None = None,
    max_cost_usd: float = 0.25,
) -> ActionProposal:
    """Judgment-only helper: propose one action without touching a page.

    This is the `judgment-only` seam a future Harness adapter uses; it cannot
    execute anything, so it is safe to expose to a host that owns execution.
    """
    if deadline is None:
        deadline = _clock() + 30.0
    state = judgment_state(task=goal, snapshot=snapshot, history=[])
    questions = step_questions(build_criteria(snapshot.action_space))
    cost_budget = _CostBudget(max_cost_usd)
    try:
        cost_budget.reserve(transport, {"state": state, "questions": questions})
    except _CostBudgetError as error:
        raise BudgetExceededError(str(error)) from error
    reply = await _await_before_deadline(
        transport.judge(state=state, questions=questions, deadline=deadline), deadline
    )
    judgment = validate_step_reply(
        reply,
        action_space=snapshot.action_space,
        snapshot=snapshot,
        goal_revision=goal_revision,
    )
    return parse_proposal(judgment, action_space=snapshot.action_space, goal=goal)
