"""Strict public contracts for jev-browser-py.

Every model here is frozen, rejects extra fields, and rejects non-finite
numbers. Model replies and page-derived data cross these boundaries, so a
contract violation is always a hard error rather than a coercion.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

__all__ = [
    "ActionCandidate",
    "ActionKind",
    "ActionOutcome",
    "ActionOutcomeState",
    "ActionProposal",
    "ActionSpace",
    "BudgetExceededError",
    "CancelToken",
    "CancelledError",
    "ChallengeError",
    "ErrorCode",
    "Evidence",
    "EvidenceRef",
    "InvalidJudgmentError",
    "JevBrowserError",
    "Judgment",
    "NetworkGuardError",
    "PolicyDeniedError",
    "RedactionStatus",
    "RunLimits",
    "RunResult",
    "RunStatus",
    "SideEffectClass",
    "SnapshotTooLargeError",
    "StaleTargetError",
    "StepTrace",
    "TransportError",
    "UnknownEffectError",
    "Usage",
    "VerificationResult",
]


class ErrorCode(StrEnum):
    """Stable failure taxonomy shared by the library and the CLI."""

    INVALID_JUDGMENT = "INVALID_JUDGMENT"
    STALE_TARGET = "STALE_TARGET"
    POLICY_DENIED = "POLICY_DENIED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CHALLENGE = "CHALLENGE"
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PAGE_TOO_LARGE = "PAGE_TOO_LARGE"
    REDACTION_FAILED = "REDACTION_FAILED"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    ERROR = "ERROR"


class RunStatus(StrEnum):
    """Terminal status of one run. Only SUCCESS implies verified completion."""

    SUCCESS = "success"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    STUCK = "stuck"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN_EFFECT = "unknown_effect"
    INVALID_JUDGMENT = "invalid_judgment"
    ERROR = "error"


class ActionKind(StrEnum):
    """Executable action kinds. One proposal resolves to exactly one kind."""

    CLICK = "click"
    TYPE = "type"
    SEARCH = "search"
    SELECT = "select"
    SUBMIT = "submit"


class ControlAction(StrEnum):
    """Non-element actions offered alongside element candidates."""

    SCROLL_DOWN = "scroll_down"
    SCROLL_UP = "scroll_up"
    BACK = "back"
    DONE = "done"


class ActionOutcomeState(StrEnum):
    """What the executor observed. API success is never task success."""

    OBSERVED = "observed"
    ACTED = "acted"
    STALE = "stale"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SideEffectClass(StrEnum):
    """Authorization class of an action, used by the policy gate."""

    READ = "read"
    LOCAL_INPUT = "local_input"
    AMBIGUOUS = "ambiguous"
    EXTERNAL_WRITE = "external_write"


class RedactionStatus(StrEnum):
    NO_SECRETS = "no_secrets"
    REDACTED = "redacted"
    FAILED = "failed"


class JevBrowserError(Exception):
    """Base error carrying a stable error code; never carries secret payloads."""

    code: ErrorCode = ErrorCode.ERROR

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidJudgmentError(JevBrowserError):
    code = ErrorCode.INVALID_JUDGMENT


class StaleTargetError(JevBrowserError):
    code = ErrorCode.STALE_TARGET


class PolicyDeniedError(JevBrowserError):
    code = ErrorCode.POLICY_DENIED


class TransportError(JevBrowserError):
    code = ErrorCode.TRANSPORT_FAILED


class UnknownEffectError(JevBrowserError):
    code = ErrorCode.UNKNOWN_EFFECT


class SnapshotTooLargeError(JevBrowserError):
    code = ErrorCode.PAGE_TOO_LARGE


class NetworkGuardError(JevBrowserError):
    code = ErrorCode.NETWORK_BLOCKED


class BudgetExceededError(JevBrowserError):
    code = ErrorCode.BUDGET_EXCEEDED


class CancelledError(JevBrowserError):
    code = ErrorCode.CANCELLED


class ChallengeError(JevBrowserError):
    code = ErrorCode.CHALLENGE


def _finite_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("must be finite")
    return number


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer")
    if value < 0:
        raise ValueError("must be non-negative")
    return value


def _unit_float(value: Any) -> float:
    number = _finite_float(value)
    if number < 0.0 or number > 1.0:
        raise ValueError("must be within [0, 1]")
    return number


FiniteFloat = Annotated[float, BeforeValidator(_finite_float)]
UnitFloat = Annotated[float, BeforeValidator(_unit_float)]
NonNegativeInt = Annotated[int, BeforeValidator(_non_negative_int)]


class StrictModel(BaseModel):
    """Frozen, extra-free base model used for every cross-boundary DTO."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Usage(StrictModel):
    """Token accounting reported by the judgment provider."""

    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0

    def estimated_cost_usd(self, price_per_mtok_input: float = 0.042) -> float:
        """Published jev input pricing is $0.042/M tokens; output tokens are free."""
        return (self.input_tokens / 1_000_000.0) * price_per_mtok_input


class ActionCandidate(StrictModel):
    """One element offered to the judgment model, valid only for its snapshot.

    `stamp` is the DOM attribute value written by the snapshotter
    (`data-jev-id="jN"`). It is stored separately from `id` because pruning
    happens *after* stamping: a noise element may consume `j1`, so the first
    offered candidate is often `j2`. Deriving one from the other would click
    the wrong element.
    """

    id: str = Field(pattern=r"^e\d{1,4}$")
    kind: ActionKind
    description: str
    fingerprint: str
    stamp: str = Field(default="", pattern=r"^(j\d{1,5})?$")
    action_key: str = Field(pattern=r"^(click|type|search|select|submit)_e\d{1,4}$")
    enabled: bool = True
    visible: bool = True
    options: tuple[str, ...] = ()
    option_indices: tuple[int, ...] = ()
    submit_via: Literal["click", "enter"] | None = None
    target_tag: str = ""
    target_role: str = ""
    target_type: str = ""
    form_method: Literal["get", "post"] | None = None
    is_navigation: bool = False
    search_field: bool = False

    @field_validator("description")
    @classmethod
    def _bounded_description(cls, value: str) -> str:
        if not value or len(value) > 300:
            raise ValueError("description must be 1..300 characters")
        return value

    @field_validator("options", "option_indices")
    @classmethod
    def _bounded_options(cls, value: tuple[str, ...] | tuple[int, ...]) -> tuple[Any, ...]:
        if len(value) > 200:
            raise ValueError("at most 200 options may be offered")
        return value


class ActionSpace(StrictModel):
    """The bounded set of actions offered for one snapshot."""

    candidates: tuple[ActionCandidate, ...]
    truncated: bool = False
    element_budget: int = 240

    def by_action_key(self, action_key: str) -> ActionCandidate | None:
        for candidate in self.candidates:
            if candidate.action_key == action_key:
                return candidate
        return None

    def action_keys(self) -> tuple[str, ...]:
        return tuple(candidate.action_key for candidate in self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)


class PageSnapshot(StrictModel):
    """Bounded, redacted observation of one page at one generation."""

    snapshot_id: str
    generation: int = Field(ge=0)
    url: str
    title: str
    observed_at: float
    excerpt: str
    frame_ref: str | None = None
    action_space: ActionSpace
    truncated: bool = False
    redaction: RedactionStatus = RedactionStatus.NO_SECRETS
    html_bytes: int = 0
    page_fingerprint: str = ""

    def action_keys(self) -> tuple[str, ...]:
        return self.action_space.action_keys()


class Judgment(StrictModel):
    """A strictly validated judgment, still a proposal and never a permission."""

    action: str
    goal_done: UnitFloat
    stuck: UnitFloat
    probabilities: dict[str, float]
    confidence: UnitFloat | None = None
    usage: Usage
    model: str
    provider: str
    snapshot_id: str
    goal_revision: int = Field(ge=0)

    @field_validator("model", "provider", "snapshot_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        if len(value) > 200:
            raise ValueError("must be at most 200 characters")
        return value

    @field_validator("probabilities")
    @classmethod
    def _probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("probabilities must not be empty")
        cleaned: dict[str, float] = {}
        total = 0.0
        for key, probability in value.items():
            if not isinstance(probability, int | float) or isinstance(probability, bool):
                raise ValueError("probabilities must be real numbers")
            number = float(probability)
            if not math.isfinite(number) or number < 0.0 or number > 1.0:
                raise ValueError("probabilities must be finite within [0, 1]")
            cleaned[key] = number
            total += number
        if abs(total - 1.0) > 0.05 + 1e-9:
            raise ValueError("probabilities must sum to approximately 1")
        return cleaned


class ActionProposal(StrictModel):
    """A judgment bound to exactly one executable action and its side effect."""

    judgment: Judgment
    candidate: ActionCandidate | None = None
    control: ControlAction | None = None
    side_effect: SideEffectClass
    snapshot_id: str
    goal_revision: int = Field(ge=0)

    @property
    def action_key(self) -> str:
        return self.judgment.action

    def is_done(self) -> bool:
        return self.judgment.action == ControlAction.DONE.value


class ActionOutcome(StrictModel):
    """Result of executing at most one action."""

    state: ActionOutcomeState
    action_key: str
    detail: str
    before_snapshot_id: str
    after_snapshot_id: str | None = None
    error_code: ErrorCode | None = None
    side_effect: SideEffectClass = SideEffectClass.READ
    effect_possible: bool = False


class EvidenceRef(StrictModel):
    """Pointer to evidence stored elsewhere; never the secret payload itself."""

    kind: str
    ref: str
    detail: str = ""


class Evidence(StrictModel):
    """Structured extraction evidence with provenance."""

    url: str
    extracted_at: float
    selector: str
    source_sha256: str
    values: tuple[str, ...]
    truncated: bool = False


class VerificationResult(StrictModel):
    """Independent outcome check. `verified` is the only success authority."""

    verified: bool
    detail: str
    evidence: tuple[EvidenceRef, ...] = ()


class StepTrace(StrictModel):
    """Redacted per-step record, safe to retain and display."""

    step: int = Field(ge=1)
    snapshot_id: str
    generation: int = Field(ge=0)
    proposed_action: str
    executed_action: str | None = None
    outcome: str
    detail: str = ""
    action_error: str | None = None
    confidence: UnitFloat | None = None
    goal_done: UnitFloat | None = None
    stuck: UnitFloat | None = None
    side_effect: SideEffectClass | None = None
    model: str | None = None


class RunLimits(StrictModel):
    """Finite, enforced run budget. Every limit is mandatory and positive."""

    max_steps: int = Field(default=12, ge=1, le=200)
    max_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_jev_calls: int = Field(default=20, ge=1, le=500)
    max_cost_usd: float = Field(default=0.25, gt=0, le=100)
    max_page_bytes: int = Field(default=2_000_000, ge=10_000, le=50_000_000)
    max_actions_per_run: int = Field(default=12, ge=1, le=200)
    max_actions_per_step: Literal[1] = 1


class RunResult(StrictModel):
    """Terminal run report. `verified` alone marks the goal as achieved."""

    status: RunStatus
    reason: str
    verified: bool = False
    steps: int = Field(default=0, ge=0)
    usage: Usage = Usage()
    jev_calls: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    final_url: str = ""
    final_snapshot_id: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    trace: tuple[StepTrace, ...] = ()
    error_code: ErrorCode | None = None


class CancelToken:
    """Cooperative cancellation for one run; safe to await from many tasks."""

    def __init__(self) -> None:
        import asyncio

        self._event = asyncio.Event()
        self._reason = "cancelled"

    def cancel(self, reason: str = "cancelled") -> None:
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError(self._reason, code=ErrorCode.CANCELLED)
