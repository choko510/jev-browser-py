"""Redacted telemetry and artifact storage.

Everything written here is already redacted upstream by `Redactor`; this
module adds a second boundary (a final deep redaction pass) and records *what
happened* rather than page bodies. A trace directory is opt-in, and the caller
supplies the redactor so the same secrets are scrubbed everywhere.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import TYPE_SAFE_INPUT_USD_PER_MTOK, RunResult, StepTrace
from .redaction import REDACTED, RedactionFailure, Redactor, build_redactor

__all__ = [
    "PRICE_PER_MTOK_IN",
    "PublicRunReport",
    "Telemetry",
    "TraceStore",
    "run_summary",
]

PRICE_PER_MTOK_IN = TYPE_SAFE_INPUT_USD_PER_MTOK


@dataclass
class Telemetry:
    """In-memory counters for one run; safe to include in a report."""

    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    steps: int = 0
    actions: int = 0
    started_at: float = field(default_factory=time.time)

    def record_judgment(self, *, input_tokens: int, output_tokens: int) -> None:
        self.jev_calls += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)

    @property
    def estimated_cost_usd(self) -> float:
        return (self.input_tokens / 1_000_000.0) * PRICE_PER_MTOK_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "jev_calls": self.jev_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "steps": self.steps,
            "actions": self.actions,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
        }


@dataclass
class PublicRunReport:
    """A serializable view of a :class:`RunResult` with no page content."""

    status: str
    reason: str
    verified: bool
    steps: int
    jev_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    elapsed_seconds: float
    final_url: str
    trace: tuple[StepTrace, ...] = ()
    evidence_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "verified": self.verified,
            "steps": self.steps,
            "jev_calls": self.jev_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "final_url": self.final_url,
            "evidence_count": self.evidence_count,
            "trace": [
                {
                    "step": step.step,
                    "proposed_action": step.proposed_action,
                    "executed_action": step.executed_action,
                    "outcome": step.outcome,
                    "detail": step.detail,
                    "goal_done": step.goal_done,
                    "stuck": step.stuck,
                }
                for step in self.trace
            ],
        }


def run_summary(result: RunResult) -> PublicRunReport:
    """Build the redacted public report for a finished run."""
    return PublicRunReport(
        status=result.status.value,
        reason=result.reason,
        verified=result.verified,
        steps=result.steps,
        jev_calls=result.jev_calls,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        estimated_cost_usd=(
            result.estimated_cost_usd if result.estimated_cost_usd is not None else result.usage.estimated_cost_usd()
        ),
        elapsed_seconds=result.elapsed_seconds,
        final_url=result.final_url,
        trace=result.trace,
        evidence_count=len(result.evidence),
    )


@dataclass
class TraceStore:
    """Writes redacted run artifacts to a caller-chosen directory."""

    directory: Path
    redactor: Redactor = field(default_factory=lambda: build_redactor([]))

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write_report(self, result: RunResult, *, name: str = "run.json") -> Path:
        report = run_summary(result)
        payload = self.redactor.redact_mapping(report.as_dict())
        path = self.directory / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def write_json(self, payload: Mapping[str, Any], *, name: str) -> Path:
        try:
            cleaned = self.redactor.redact_mapping(payload)
        except RedactionFailure:
            cleaned = {"error": REDACTED}
        path = self.directory / name
        path.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def write_text(self, text: str, *, name: str) -> Path:
        path = self.directory / name
        path.write_text(self.redactor.redact(text), encoding="utf-8")
        return path
