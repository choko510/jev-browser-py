"""Package public API for jev-browser-py.

The library exposes a small, stable surface: build a transport, hand it a
Playwright page, and read a terminal `RunResult`. Everything else — the
snapshotter, policy gate, executor, verifier, and extraction — is composed by
:func:`run` under a finite budget.

Judgment is never permission. `RunResult.status is RunStatus.SUCCESS` requires
an independent verifier, not a model's `goal_done`.
"""

from __future__ import annotations

from .action_space import RawElement, build_action_space
from .contracts import (
    ActionCandidate,
    ActionKind,
    ActionOutcome,
    ActionOutcomeState,
    ActionProposal,
    ActionSpace,
    BudgetExceededError,
    CancelledError,
    CancelToken,
    ChallengeError,
    ErrorCode,
    Evidence,
    EvidenceRef,
    InvalidJudgmentError,
    Judgment,
    NetworkGuardError,
    PageSnapshot,
    PolicyDeniedError,
    RedactionStatus,
    RunLimits,
    RunResult,
    RunStatus,
    SideEffectClass,
    SnapshotTooLargeError,
    StaleTargetError,
    StepTrace,
    TransportError,
    UnknownEffectError,
    Usage,
    VerificationResult,
)
from .judgment import (
    assert_fresh as assert_fresh,
)
from .judgment import (
    parse_proposal as parse_proposal,
)
from .judgment import (
    validate_select_reply as validate_select_reply,
)
from .judgment import (
    validate_step_reply as validate_step_reply,
)
from .policy import (
    ActionPolicy,
    PolicyDecision,
    PolicyRule,
    default_policy,
)
from .questions import (
    build_criteria as build_criteria,
)
from .questions import (
    select_option_question as select_option_question,
)
from .questions import (
    step_questions as step_questions,
)
from .redaction import (
    Redactor,
)
from .redaction import (
    build_redactor as build_redactor,
)
from .transport import (
    JevTransport,
    MockTransport,
    ScriptedTransport,
    TransportReply,
    TypeSafeTransport,
)

__version__ = "1.0.0"

_PUBLIC = [
    "ActionCandidate",
    "ActionKind",
    "ActionOutcome",
    "ActionOutcomeState",
    "ActionPolicy",
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
    "JevTransport",
    "Judgment",
    "MockTransport",
    "NetworkGuardError",
    "PageSnapshot",
    "PolicyDecision",
    "PolicyDeniedError",
    "PolicyRule",
    "RawElement",
    "RedactionStatus",
    "Redactor",
    "RunLimits",
    "RunResult",
    "RunStatus",
    "ScriptedTransport",
    "SideEffectClass",
    "SnapshotTooLargeError",
    "StaleTargetError",
    "StepTrace",
    "TransportError",
    "TransportReply",
    "TypeSafeTransport",
    "UnknownEffectError",
    "Usage",
    "VerificationResult",
    "assert_fresh",
    "build_action_space",
    "build_criteria",
    "build_redactor",
    "default_policy",
    "parse_proposal",
    "select_option_question",
    "step_questions",
    "validate_select_reply",
    "validate_step_reply",
]

__all__ = [*_PUBLIC, "__version__"]


def _lazy(name: str):  # type: ignore[no-untyped-def]
    """Import heavy or optional submodules only when first used."""
    import importlib

    module = importlib.import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


_LAZY_EXPORTS = {
    "run": "runtime",
    "suggest_next_action": "runtime",
    "snapshot_page": "snapshot",
    "Snapshotter": "snapshot",
    "extract_structured": "extraction",
    "ExtractionResult": "extraction",
    "BrowserSession": "session",
    "BrowserOwner": "session",
    "open_session": "session",
    "Verifier": "verifier",
    "RuleBasedVerifier": "verifier",
    "verify_outcome": "verifier",
    "TypeSafeTextGenerator": "typing",
    "TextGenerator": "typing",
}


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_lazy(module_name), name)
