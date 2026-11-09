"""Test doubles for the browser seam.

A real Playwright browser is exercised by `test_e2e_local.py`; everything else
uses these fakes so unit tests are fast, deterministic, and browser-free.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jev_browser_py.contracts import (
    ActionOutcome,
    ActionOutcomeState,
    ActionProposal,
    PageSnapshot,
    SideEffectClass,
)
from jev_browser_py.typing import GeneratedText


class FakeSnapshotter:
    """Replays a scripted list of snapshots, then repeats the last one."""

    def __init__(self, snapshots: Sequence[PageSnapshot]) -> None:
        if not snapshots:
            raise AssertionError("FakeSnapshotter needs at least one snapshot")
        self._snapshots = list(snapshots)
        self._index = 0
        self._generation = snapshots[0].generation
        self.observed: list[int] = []
        from jev_browser_py.redaction import Redactor

        self.redactor = Redactor([])

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    def ensure_secrets(self, secrets: Sequence[str]) -> None:
        """Mirror the real snapshotter's redactor wiring."""
        from jev_browser_py.redaction import build_redactor

        if secrets:
            self.redactor = build_redactor(list(secrets))

    async def observe(self, *, bump: bool = False) -> PageSnapshot:
        if bump:
            self.bump_generation()
        snapshot = self._snapshots[min(self._index, len(self._snapshots) - 1)]
        self._index += 1
        self.observed.append(self._index)
        return snapshot.model_copy(update={"generation": self._generation})


@dataclass
class FakeExecutor:
    """Records proposals and returns scripted outcomes."""

    outcomes: Sequence[ActionOutcome | Exception] = field(default_factory=list)
    executed: list[ActionProposal] = field(default_factory=list)
    typed_texts: list[str | None] = field(default_factory=list)
    select_indices: list[int | None] = field(default_factory=list)

    async def execute_one(
        self,
        *,
        proposal: ActionProposal,
        snapshot: PageSnapshot,
        current_snapshot: PageSnapshot | None = None,
        typed_text: str | None = None,
        select_option_index: int | None = None,
    ) -> ActionOutcome:
        del current_snapshot
        self.executed.append(proposal)
        self.typed_texts.append(typed_text)
        self.select_indices.append(select_option_index)
        index = len(self.executed) - 1
        if index < len(self.outcomes):
            outcome = self.outcomes[index]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return ActionOutcome(
            state=ActionOutcomeState.ACTED,
            action_key=proposal.action_key,
            detail="acted",
            before_snapshot_id=snapshot.snapshot_id,
            side_effect=proposal.side_effect,
        )

    @property
    def action_count(self) -> int:
        return len(self.executed)


def acted(proposal_action: str, *, snapshot_id: str = "snap-1", effect: bool = False) -> ActionOutcome:
    return ActionOutcome(
        state=ActionOutcomeState.ACTED,
        action_key=proposal_action,
        detail="acted on the fixture",
        before_snapshot_id=snapshot_id,
        side_effect=SideEffectClass.READ,
        effect_possible=effect,
    )


def uncertain(proposal_action: str, *, snapshot_id: str = "snap-1") -> ActionOutcome:
    return ActionOutcome(
        state=ActionOutcomeState.UNCERTAIN,
        action_key=proposal_action,
        detail="the effect is unknown",
        before_snapshot_id=snapshot_id,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        effect_possible=True,
    )


def stale(proposal_action: str, *, snapshot_id: str = "snap-1") -> ActionOutcome:
    return ActionOutcome(
        state=ActionOutcomeState.STALE,
        action_key=proposal_action,
        detail="the target went stale",
        before_snapshot_id=snapshot_id,
    )


@dataclass
class RecordingTextGenerator:
    text: str = "sample query"
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    provider: str = "recording"

    async def generate(self, *, task: str, element_description: str, url: str) -> GeneratedText:
        self.calls.append((task, element_description, url))
        return GeneratedText(text=self.text, via=self.provider)


class FakePage:
    """Minimal Playwright page stand-in for the executor's small surface."""

    def __init__(self, *, url: str = "https://example.test/") -> None:
        self.url = url
        self.calls: list[tuple[str, Any]] = []
        self.fail_with: Exception | None = None
        self.element_count = 1
        self.preflight_info: dict[str, Any] = {
            "tag": "button",
            "type": "",
            "visible": True,
            "enabled": True,
            "readOnly": False,
        }

    async def click(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("click", selector))
        if self.fail_with is not None:
            raise self.fail_with

    async def fill(self, selector: str, value: str, timeout: int | None = None) -> None:
        self.calls.append(("fill", (selector, value)))
        if self.fail_with is not None:
            raise self.fail_with

    async def press(self, selector: str, key: str, timeout: int | None = None) -> None:
        self.calls.append(("press", (selector, key)))
        if self.fail_with is not None:
            raise self.fail_with

    async def select_option(self, selector: str, *, index: int, timeout: int | None = None) -> None:
        self.calls.append(("select_option", (selector, index)))
        if self.fail_with is not None:
            raise self.fail_with

    async def go_back(self, wait_until: str | None = None, timeout: int | None = None) -> bool:
        self.calls.append(("go_back", wait_until))
        return True

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.calls.append(("evaluate", arg))
        if "window.scrollBy" in script:
            return None
        if "document.querySelector(sel)" in script:
            return self.preflight_info
        if "const el = document.querySelector(sel);" in script:
            return self.preflight_info
        return f"fingerprint-{len(self.calls)}"

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        return None

    def locator(self, selector: str) -> Any:
        page = self

        class _Locator:
            async def count(self) -> int:
                page.calls.append(("locator.count", selector))
                return page.element_count

        return _Locator()
