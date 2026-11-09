"""Deny-by-default action authorization.

`ActionPolicy` is a pure decision function over a proposal and the live
snapshot. The default policy implements the guide's MUST rules:

* Read-only and local-input actions are allowed.
* `ambiguous` actions (login/accept/continue, unlabeled controls) are denied
  unless the caller explicitly grants that class.
* `external_write` actions (submit, purchase, post, delete, send) always
  require an explicit grant. A high model confidence never substitutes for it.

The policy authors *permission*; the executor still re-checks freshness and
element uniqueness immediately before touching the page.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .contracts import (
    ActionCandidate,
    ActionKind,
    ActionProposal,
    ControlAction,
    ErrorCode,
    PageSnapshot,
    PolicyDeniedError,
    SideEffectClass,
)

__all__ = [
    "HIGH_RISK_KINDS",
    "ActionPolicy",
    "AllowAllReadPolicy",
    "PolicyDecision",
    "PolicyRule",
    "default_policy",
]

HIGH_RISK_KINDS = frozenset({ActionKind.SUBMIT})


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The authorization result for one proposal."""

    allowed: bool
    reason: str
    side_effect: SideEffectClass

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise PolicyDeniedError(self.reason)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """An allow-list entry for one side-effect class."""

    side_effect: SideEffectClass
    allow: bool = True


@dataclass
class ActionPolicy:
    """Configurable deny-by-default policy.

    `allow` names the side-effect classes the *caller* has approved. By default
    only `read` and `local_input` are granted; `ambiguous` and `external_write`
    must be opted into explicitly, which is the human-approval seam.
    """

    allow: frozenset[SideEffectClass] = field(
        default_factory=lambda: frozenset({SideEffectClass.READ, SideEffectClass.LOCAL_INPUT})
    )
    deny_actions: frozenset[str] = field(default_factory=frozenset)
    deny_kinds: frozenset[ActionKind] = field(default_factory=frozenset)

    async def authorize(self, *, proposal: ActionProposal, snapshot: PageSnapshot) -> PolicyDecision:
        return self.decide(proposal=proposal, snapshot=snapshot)

    def decide(self, *, proposal: ActionProposal, snapshot: PageSnapshot) -> PolicyDecision:
        """Pure decision; no I/O, so it is trivially auditable and testable."""
        if proposal.snapshot_id != snapshot.snapshot_id:
            return PolicyDecision(
                allowed=False,
                reason="proposal is bound to a different snapshot",
                side_effect=proposal.side_effect,
            )
        if proposal.action_key in self.deny_actions:
            return PolicyDecision(
                allowed=False,
                reason=f"action {proposal.action_key} is explicitly denied",
                side_effect=proposal.side_effect,
            )
        candidate = proposal.candidate
        if candidate is not None:
            if candidate.kind in self.deny_kinds:
                return PolicyDecision(
                    allowed=False,
                    reason=f"action kind {candidate.kind.value} is denied by policy",
                    side_effect=proposal.side_effect,
                )
            if not candidate.enabled or not candidate.visible:
                return PolicyDecision(
                    allowed=False,
                    reason="target is not enabled and visible",
                    side_effect=proposal.side_effect,
                )
        control = proposal.control
        if control is ControlAction.DONE:
            return PolicyDecision(
                allowed=False,
                reason="'done' is a stop gate, not an executable action",
                side_effect=proposal.side_effect,
            )
        if proposal.side_effect not in self.allow:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"side effect {proposal.side_effect.value} needs explicit approval; "
                    "model confidence is not authorization"
                ),
                side_effect=proposal.side_effect,
            )
        return PolicyDecision(
            allowed=True,
            reason=f"side effect {proposal.side_effect.value} is approved",
            side_effect=proposal.side_effect,
        )


@runtime_checkable
class ActionAuthorizer(Protocol):
    """Host seam: a future Harness Gateway/Sentinel adapter implements this."""

    async def authorize(self, *, proposal: ActionProposal, snapshot: PageSnapshot) -> PolicyDecision: ...


def default_policy(
    *,
    allow_ambiguous: bool = False,
    allow_external_write: bool = False,
    deny_kinds: Iterable[ActionKind] = (),
    deny_actions: Iterable[str] = (),
) -> ActionPolicy:
    """Build the default policy, optionally opening riskier classes."""
    classes: set[SideEffectClass] = {SideEffectClass.READ, SideEffectClass.LOCAL_INPUT}
    if allow_ambiguous:
        classes.add(SideEffectClass.AMBIGUOUS)
    if allow_external_write:
        classes.add(SideEffectClass.EXTERNAL_WRITE)
    return ActionPolicy(
        allow=frozenset(classes),
        deny_actions=frozenset(deny_actions),
        deny_kinds=frozenset(deny_kinds),
    )


class AllowAllReadPolicy(ActionPolicy):
    """Convenience policy for read-only browsing with fills.

    Still denies `ambiguous` and `external_write`. Named for intent: this is
    what an unprivileged read-only task should use.
    """

    def __init__(self) -> None:
        super().__init__()


def describe_denial(candidate: ActionCandidate | None) -> str:
    """Short, secret-free explanation used in traces."""
    if candidate is None:
        return "no element target"
    return f"{candidate.kind.value} on {candidate.id}: {candidate.description[:80]}"


def denial_error(decision: PolicyDecision) -> PolicyDeniedError:
    return PolicyDeniedError(decision.reason, code=ErrorCode.POLICY_DENIED)
