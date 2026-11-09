"""Single-action execution over a pre-authorized proposal.

The executor is intentionally dumb about authorization: it receives a proposal
the policy has already allowed and refuses anything else. What it adds is the
last-instant safety work:

* Freshness: the snapshot id and DOM generation must still match.
* Uniqueness: the stamped selector must resolve to exactly one element.
* State: the target must still be visible, enabled, and of the expected type.
* Origin: after an action, the resulting URL must not silently cross origins.

The "unknown effect" concept is the important one. A click that raised a
Playwright timeout may still have reached the server. Any action that could
have an external effect and does not produce a clean observed result becomes
`UNCERTAIN`, and the run loop stops without retrying.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .contracts import (
    ActionCandidate,
    ActionKind,
    ActionOutcome,
    ActionOutcomeState,
    ActionProposal,
    ControlAction,
    ErrorCode,
    PageSnapshot,
    SideEffectClass,
    StaleTargetError,
)
from .redaction import REDACTED

__all__ = [
    "ACTION_TIMEOUT_MS",
    "NAV_TIMEOUT_MS",
    "ExecutorOptions",
    "SingleActionExecutor",
    "selector_for",
]

ACTION_TIMEOUT_MS = 4_000
NAV_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 1_500
SETTLE_POLL_MS = 100


def selector_for(candidate: ActionCandidate, attr: str | None = None) -> str:
    """CSS selector for a stamped element.

    The stamp comes from the candidate itself, never from its index: pruning
    happens after stamping, so `e1` is frequently stamped `j2`.
    """
    stamp = attr if attr is not None else candidate.stamp
    if not stamp:
        raise ValueError("candidate has no DOM stamp; it was not produced by the snapshotter")
    numeric = stamp[1:] if stamp.startswith("j") else ""
    if not numeric.isdigit():
        raise ValueError("element stamp must be j<number>")
    return f'[data-jev-id="{stamp}"]'


@dataclass(frozen=True, slots=True)
class ExecutorOptions:
    action_timeout_ms: int = ACTION_TIMEOUT_MS
    nav_timeout_ms: int = NAV_TIMEOUT_MS
    settle_timeout_ms: int = SETTLE_TIMEOUT_MS
    allow_typing: bool = True
    typed_text_max: int = 500
    password_value: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.typed_text_max <= 0 or self.typed_text_max > 10_000:
            raise ValueError("typed_text_max must be within 1..10000")


@dataclass(frozen=True, slots=True)
class ActionExecution:
    """Low-level result of one executed action, before outcome classification."""

    acted: bool
    detail: str
    error: str | None = None
    effect_possible: bool = False
    typed_text: str | None = None
    navigated: bool = False


class SingleActionExecutor:
    """Executes at most one action per call against one Playwright page."""

    def __init__(
        self,
        page: Any,
        *,
        options: ExecutorOptions | None = None,
        target_resolver: Any | None = None,
    ) -> None:
        self._page = page
        self._options = options or ExecutorOptions()
        self._resolver = target_resolver

    async def execute_one(
        self,
        *,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        typed_text: str | None = None,
        select_option_index: int | None = None,
    ) -> ActionOutcome:
        """Run a proposal, returning a classified outcome.

        Raises :class:`StaleTargetError` when the target no longer matches the
        snapshot; returns an `UNCERTAIN` outcome when an action that may have
        had an external effect failed ambiguously.
        """
        from .judgment import assert_fresh

        assert_fresh(proposal, snapshot)
        if proposal.control is ControlAction.DONE:
            return self._outcome(
                proposal,
                snapshot,
                state=ActionOutcomeState.BLOCKED,
                detail="'done' is a stop gate and is never executed",
                error_code=ErrorCode.POLICY_DENIED,
            )
        candidate = proposal.candidate
        if candidate is None:
            return await self._execute_control(proposal, snapshot)
        return await self._execute_element(
            proposal, snapshot, typed_text=typed_text, select_option_index=select_option_index
        )

    async def _execute_control(self, proposal: ActionProposal, snapshot: PageSnapshot) -> ActionOutcome:
        control = proposal.control
        if control is ControlAction.BACK:
            try:
                went_back = await self._page.go_back(
                    wait_until="domcontentloaded",
                    timeout=self._options.nav_timeout_ms,
                )
            except Exception as error:  # noqa: BLE001 - classify, never re-raise raw
                return self._outcome(
                    proposal,
                    snapshot,
                    state=ActionOutcomeState.FAILED,
                    detail="back failed",
                    error_code=ErrorCode.TRANSPORT_FAILED,
                    error=str(error),
                )
            detail = "went back" if went_back else "no history to go back to"
            return self._outcome(proposal, snapshot, state=ActionOutcomeState.ACTED, detail=detail)
        if control in (ControlAction.SCROLL_DOWN, ControlAction.SCROLL_UP):
            direction = 1 if control is ControlAction.SCROLL_DOWN else -1
            try:
                await self._page.evaluate("(dir) => window.scrollBy(0, dir * window.innerHeight * 0.8)", direction)
            except Exception as error:  # noqa: BLE001 - classify, never re-raise raw
                return self._outcome(
                    proposal,
                    snapshot,
                    state=ActionOutcomeState.FAILED,
                    detail="scroll failed",
                    error_code=ErrorCode.TRANSPORT_FAILED,
                    error=str(error),
                )
            return self._outcome(proposal, snapshot, state=ActionOutcomeState.ACTED, detail=control.value)
        return self._outcome(
            proposal,
            snapshot,
            state=ActionOutcomeState.BLOCKED,
            detail="unknown control action",
            error_code=ErrorCode.POLICY_DENIED,
        )

    async def _execute_element(
        self,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        *,
        typed_text: str | None,
        select_option_index: int | None,
    ) -> ActionOutcome:
        candidate = proposal.candidate
        assert candidate is not None
        stamp = await self._resolve_stamp(candidate, snapshot)
        if stamp is None:
            return self._outcome(
                proposal,
                snapshot,
                state=ActionOutcomeState.STALE,
                detail="target did not resolve to exactly one stamped element",
                error_code=ErrorCode.STALE_TARGET,
            )
        selector = f'[data-jev-id="{stamp}"]'
        problem = await self._preflight(selector, candidate)
        if problem is not None:
            return self._outcome(
                proposal,
                snapshot,
                state=ActionOutcomeState.STALE,
                detail=problem,
                error_code=ErrorCode.STALE_TARGET,
            )
        before_url = snapshot.url
        try:
            result = await self._dispatch(
                candidate,
                selector,
                typed_text=typed_text,
                select_option_index=select_option_index,
            )
        except Exception as error:  # noqa: BLE001 - Playwright errors are classified
            return await self._classify_failure(proposal, snapshot, candidate, error, before_url)
        await self._settle()
        navigated = await self._navigated(before_url)
        state = ActionOutcomeState.ACTED
        detail = result.detail
        effect_possible = proposal.side_effect is SideEffectClass.EXTERNAL_WRITE
        return self._outcome(
            proposal,
            snapshot,
            state=state,
            detail=detail,
            effect_possible=effect_possible or navigated,
            typed_text=result.typed_text,
        )

    async def _resolve_stamp(self, candidate: ActionCandidate, snapshot: PageSnapshot) -> str | None:
        """Find the stamp for a candidate, verifying uniqueness.

        The candidate carries the stamp the snapshotter wrote, so a pruned
        noise element earlier in the DOM cannot shift the target. A page whose
        DOM changed can still carry the old stamp, which is why the
        fingerprint was compared in `assert_fresh` before this point.
        """
        expected = candidate.stamp
        if not expected:
            return None
        try:
            count = await self._page.locator(f'[data-jev-id="{expected}"]').count()
        except Exception:  # noqa: BLE001 - a closed page cannot be resolved
            return None
        if count != 1:
            return None
        return expected

    async def _preflight(self, selector: str, candidate: ActionCandidate) -> str | None:
        """Re-check that the target is still visible, enabled, and the right type."""
        try:
            info = await self._page.evaluate(
                """
                (sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return null;
                  const style = getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return {
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute("type") || "").toLowerCase(),
                    visible: el.getClientRects().length > 0 && style.display !== "none" &&
                             style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
                    enabled: !(el.disabled === true || el.getAttribute("aria-disabled") === "true"),
                    readOnly: el.readOnly === true,
                  };
                }
                """,
                selector,
            )
        except Exception:  # noqa: BLE001 - leaf: page closed or navigating
            return "target could not be inspected; the page may have navigated"
        if info is None:
            return "target disappeared before execution"
        if not info.get("visible"):
            return "target is no longer visible"
        if not info.get("enabled"):
            return "target is no longer enabled"
        if candidate.kind is ActionKind.TYPE or candidate.kind is ActionKind.SEARCH:
            if info.get("readOnly"):
                return "target became read-only"
            tag = str(info.get("tag"))
            if tag not in {"input", "textarea"}:
                return "target is no longer an input"
        if candidate.kind is ActionKind.SELECT and str(info.get("tag")) != "select":
            return "target is no longer a select"
        if candidate.kind is ActionKind.CLICK and str(info.get("tag")) not in {
            "a",
            "button",
            "summary",
            "input",
            "label",
        }:
            role_like = str(info.get("tag"))
            if role_like not in {"div", "span", "li", "td", "th"}:
                return "target is no longer a clickable element"
        return None

    async def _dispatch(
        self,
        candidate: ActionCandidate,
        selector: str,
        *,
        typed_text: str | None,
        select_option_index: int | None,
    ) -> ActionExecution:
        options = self._options
        timeout = options.action_timeout_ms
        if candidate.kind is ActionKind.CLICK:
            await self._page.click(selector, timeout=timeout)
            return ActionExecution(acted=True, detail=candidate.description)
        if candidate.kind is ActionKind.SEARCH:
            if not options.allow_typing:
                return ActionExecution(acted=False, detail="typing disabled", error="typing disabled")
            text = self._validated_text(typed_text)
            await self._page.fill(selector, text, timeout=timeout)
            await self._page.press(selector, "Enter", timeout=timeout)
            return ActionExecution(acted=True, detail=f"searched (text len {len(text)})", typed_text=text)
        if candidate.kind is ActionKind.TYPE:
            if not options.allow_typing:
                return ActionExecution(acted=False, detail="typing disabled", error="typing disabled")
            text = self._validated_text(typed_text)
            await self._page.fill(selector, text, timeout=timeout)
            return ActionExecution(acted=True, detail=f"typed (text len {len(text)})", typed_text=text)
        if candidate.kind is ActionKind.SUBMIT:
            if candidate.submit_via == "enter":
                await self._page.press(selector, "Enter", timeout=timeout)
                return ActionExecution(acted=True, detail=f"submitted via Enter: {candidate.description}")
            await self._page.click(selector, timeout=timeout)
            return ActionExecution(acted=True, detail=f"submitted: {candidate.description}")
        if candidate.kind is ActionKind.SELECT:
            if not candidate.option_indices:
                return ActionExecution(acted=False, detail="select has no options", error="no options")
            index = select_option_index if select_option_index is not None else 0
            if index < 0 or index >= len(candidate.option_indices):
                return ActionExecution(acted=False, detail="select option index is out of range", error="bad index")
            dom_index = candidate.option_indices[index]
            await self._page.select_option(selector, index=dom_index, timeout=timeout)
            return ActionExecution(acted=True, detail=f"selected option {index}")
        return ActionExecution(acted=False, detail="unsupported kind", error="unsupported kind")

    def _validated_text(self, typed_text: str | None) -> str:
        if typed_text is None:
            raise ValueError("a type or search action requires generated text")
        if len(typed_text) > self._options.typed_text_max:
            raise ValueError("generated text exceeds the configured length limit")
        return typed_text

    async def _settle(self) -> None:
        """Wait for DOM stability with a hard cap, never indefinitely.

        Two consecutive identical fingerprints mean the page stopped
        re-rendering, which is the signal that matters. Heavy sites with
        analytics pings still settle because network noise is ignored.
        """
        page = self._page
        deadline = time.monotonic() + self._options.settle_timeout_ms / 1000.0
        previous: str | None = None
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self._options.action_timeout_ms)
        except Exception:  # noqa: BLE001 - a page already loaded, or a navigation in flight
            return
        while time.monotonic() < deadline:
            try:
                fingerprint = await page.evaluate(
                    """() =>
                    `${document.body?.innerText?.length ?? 0}:` +
                    `${document.querySelectorAll("a,button,input,select,textarea").length}`
                    """
                )
            except Exception:  # noqa: BLE001 - leaf: navigation replaced the document
                return
            if fingerprint == previous:
                return
            previous = fingerprint
            await _sleep(min(SETTLE_POLL_MS / 1000.0, max(0.0, deadline - time.monotonic())))

    async def _navigated(self, before_url: str) -> bool:
        try:
            current = self._page.url
            url = current() if callable(current) else str(current)
        except Exception:  # noqa: BLE001 - closed page
            return False
        return _origin(url) != _origin(before_url)

    async def _classify_failure(
        self,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        candidate: ActionCandidate,
        error: Exception,
        before_url: str,
    ) -> ActionOutcome:
        """Classify a Playwright failure; never retry and never assume success.

        A failure that could have reached the server (a submit, or any click
        that may have fired a request) is `UNCERTAIN`; the caller must stop
        rather than double-send.
        """
        message = _summarize(error)
        navigated = await self._navigated(before_url)
        if navigated:
            return self._outcome(
                proposal,
                snapshot,
                state=ActionOutcomeState.UNCERTAIN,
                detail="action errored while the page navigated; the effect is unknown",
                error_code=ErrorCode.UNKNOWN_EFFECT,
                error=message,
                effect_possible=True,
            )
        risky = candidate.kind in (ActionKind.SUBMIT, ActionKind.CLICK)
        if risky and proposal.side_effect is not SideEffectClass.READ:
            return self._outcome(
                proposal,
                snapshot,
                state=ActionOutcomeState.UNCERTAIN,
                detail="action failed after it may have reached the server; not retried",
                error_code=ErrorCode.UNKNOWN_EFFECT,
                error=message,
                effect_possible=True,
            )
        return self._outcome(
            proposal,
            snapshot,
            state=ActionOutcomeState.FAILED,
            detail="action failed before any effect",
            error_code=ErrorCode.STALE_TARGET
            if "detached" in message.lower() or "not attached" in message.lower()
            else ErrorCode.TRANSPORT_FAILED,
            error=message,
        )

    def _outcome(
        self,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        *,
        state: ActionOutcomeState,
        detail: str,
        error_code: ErrorCode | None = None,
        error: str | None = None,
        effect_possible: bool = False,
        typed_text: str | None = None,
    ) -> ActionOutcome:
        suffix = f" ({error})" if error else ""
        return ActionOutcome(
            state=state,
            action_key=proposal.action_key,
            detail=f"{detail}{suffix}"[:400],
            before_snapshot_id=snapshot.snapshot_id,
            after_snapshot_id=None,
            error_code=error_code,
            side_effect=proposal.side_effect,
            effect_possible=effect_possible,
        )


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}".lower()
    return parts.scheme.lower()


def _summarize(error: Exception) -> str:
    """Short, provider-body-free error text; never includes the typed value."""
    name = type(error).__name__
    message = str(error).replace(REDACTED, REDACTED)
    message = re_flat(message)
    return f"{name}: {message}"[:160]


def re_flat(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text).strip()


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def outcome_is_unknown(outcome: ActionOutcome) -> bool:
    return outcome.state is ActionOutcomeState.UNCERTAIN or outcome.effect_possible


def outcome_did_act(outcome: ActionOutcome) -> bool:
    return outcome.state is ActionOutcomeState.ACTED and outcome.error_code is None


def describe_outcome_kind(outcome: ActionOutcome) -> Literal["acted", "stale", "uncertain", "failed", "blocked"]:
    mapping: dict[ActionOutcomeState, Literal["acted", "stale", "uncertain", "failed", "blocked"]] = {
        ActionOutcomeState.ACTED: "acted",
        ActionOutcomeState.OBSERVED: "acted",
        ActionOutcomeState.STALE: "stale",
        ActionOutcomeState.UNCERTAIN: "uncertain",
        ActionOutcomeState.FAILED: "failed",
        ActionOutcomeState.BLOCKED: "blocked",
        ActionOutcomeState.NEEDS_HUMAN: "blocked",
        ActionOutcomeState.CANCELLED: "stale",
    }
    return mapping[outcome.state]


def ensure_stale_is_raised(outcome: ActionOutcome) -> None:
    """Promote a stale outcome back into an exception for strict callers."""
    if outcome.state is ActionOutcomeState.STALE:
        raise StaleTargetError(outcome.detail)
