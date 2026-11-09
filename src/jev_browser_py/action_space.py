"""Bounded, deduplicated action space construction.

Ported in behaviour from the upstream Jev Browser `lib.ts` (`buildActionSpace`,
`isNoiseName`, `isNoiseHref`, `buildCriteria`) at commit 913d1a1, MIT licensed.
The Python version keeps the same pruning rules and adds an explicit
fingerprint per candidate so a stale proposal can be detected byte-for-byte
right before execution.

Copyright (c) 2026 Joey Kudish (upstream Jev Browser logic).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ActionCandidate, ActionKind, ActionSpace
from .questions import MAX_ELEMENTS

__all__ = [
    "MAX_ELEMENTS",
    "MAX_RAW_ELEMENTS",
    "PASSWORD_KIND",
    "RawElement",
    "build_action_space",
    "is_noise_href",
    "is_noise_name",
    "raw_from_mapping",
]

MAX_RAW_ELEMENTS = 2000
PASSWORD_KIND = "fill_password"

_JUNK_NAMES = frozenset(
    {
        "jump up",
        "jump up to",
        "jump up to:",
        "jump to content",
        "edit",
        "permalink",
        "permanent link",
        "cite this page",
        "donate",
        "create account",
        "log in",
        "talk",
        "contributions",
        "view history",
        "read",
        "source",
        "hide",
        "show",
        "skip to content",
    }
)

_CITATION_ONLY = re.compile(r"^[\d\s.,:;()\[\]-]+$")


def is_noise_name(name: str) -> bool:
    """Upstream-compatible noise filter for element labels."""
    lowered = " ".join(name.split()).lower()
    if not lowered or len(lowered) > 80:
        return True
    if lowered in _JUNK_NAMES:
        return True
    return bool(_CITATION_ONLY.match(lowered))


def is_noise_href(href: str) -> bool:
    """Upstream-compatible noise filter for link targets."""
    if not href:
        return False
    if href.startswith("#"):
        return True
    if href.startswith("javascript:"):
        return True
    if href.startswith(("mailto:", "tel:")):
        return True
    return "action=edit" in href


@dataclass(frozen=True, slots=True)
class RawElement:
    """One interactive candidate as extracted from the page.

    `attr` is the stamp value written into the DOM; it is intentionally
    ephemeral and regenerated on every observation.
    """

    attr: str
    tag: str
    role: str
    text: str
    href: str = ""
    type_attr: str = ""
    clickable: bool = False
    typeable: bool = False
    selectable: bool = False
    password_input: bool = False
    search_field: bool = False
    submit_control: bool = False
    enter_submittable: bool = False
    options: tuple[tuple[int, str], ...] = ()
    state: tuple[str, ...] = ()
    enabled: bool = True

    def fingerprint(self) -> str:
        """Stable digest of the properties that make this target unique.

        Deliberately excludes `attr`, which is regenerated on every snapshot,
        and excludes any field value so secrets never enter a digest input.
        """
        payload = "\x1f".join(
            [
                self.tag,
                self.role,
                self.text,
                self.href,
                self.type_attr,
                "1" if self.clickable else "0",
                "1" if self.typeable else "0",
                "1" if self.selectable else "0",
                "1" if self.search_field else "0",
                "1" if self.submit_control else "0",
                "1" if self.enter_submittable else "0",
                "1" if self.enabled else "0",
                "\x1e".join(f"{index}:{label}" for index, label in self.options),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def raw_from_mapping(payload: Mapping[str, Any]) -> RawElement:
    """Build a :class:`RawElement` from a browser-evaluated JSON object."""
    options_payload = payload.get("options") or ()
    options: list[tuple[int, str]] = []
    for option in options_payload:
        if isinstance(option, Mapping):
            index = option.get("i")
            label = option.get("label")
            if isinstance(index, int) and not isinstance(index, bool) and isinstance(label, str):
                options.append((index, label))
        elif isinstance(option, (list, tuple)) and len(option) == 2:
            index, label = option
            if isinstance(index, int) and isinstance(label, str):
                options.append((index, label))
    state_payload = payload.get("state") or ()
    return RawElement(
        attr=str(payload.get("attr") or ""),
        tag=str(payload.get("tag") or ""),
        role=str(payload.get("role") or ""),
        text=str(payload.get("text") or ""),
        href=str(payload.get("href") or ""),
        type_attr=str(payload.get("typeAttr") or ""),
        clickable=bool(payload.get("clickable")),
        typeable=bool(payload.get("typeable")),
        selectable=bool(payload.get("selectable")),
        password_input=bool(payload.get("passwordInput")),
        search_field=bool(payload.get("searchField")),
        submit_control=bool(payload.get("submitControl")),
        enter_submittable=bool(payload.get("enterSubmittable")),
        options=tuple(options),
        state=tuple(str(item) for item in state_payload),
        enabled=bool(payload.get("enabled", True)),
    )


def _describe(kind: ActionKind, control: str, label: str, element: RawElement) -> str:
    if kind is ActionKind.SEARCH:
        return f'{control} "{label}" (type into this search box and run the search)'
    if kind is ActionKind.SUBMIT:
        return f'{control} "{label}" (submit the form now)'
    if kind is ActionKind.TYPE:
        return f'{control} "{label}" (type without submitting)'
    if kind is ActionKind.SELECT:
        return f'{control} "{label}" (dropdown; a follow-up picks the option)'
    state_tail = f" ({', '.join(element.state)})" if element.state else ""
    href = element.href
    href_tail = f" -> {re.sub(r'^https?://', '', href)[:70]}" if href else ""
    return f'{control} "{label}"{state_tail}{href_tail}'


def _classify(element: RawElement) -> ActionKind | None:
    if element.search_field:
        return ActionKind.SEARCH
    if element.submit_control:
        return ActionKind.SUBMIT
    if element.typeable:
        return ActionKind.TYPE
    if element.selectable:
        return ActionKind.SELECT
    if element.clickable:
        return ActionKind.CLICK
    return None


def build_action_space(
    raw: Sequence[RawElement],
    *,
    password_active: bool = False,
    cap: int = MAX_ELEMENTS,
) -> ActionSpace:
    """Filter, dedupe by destination, cap, and describe one step's actions.

    Rules preserved from upstream:

    * Password inputs are offered only when a credential source is active.
    * Noise labels and noise hrefs are dropped.
    * Password/file inputs are never offered as type targets.
    * Duplicate link destinations collapse to one candidate.
    * Submit controls appear as `submit_eN`, never `click_eN`, so a form
      submission is always an explicit decision.
    * A single-line text field also gets a `submit` twin (Enter) so
      Enter-driven flows stay reachable as two explicit steps.
    """
    limit = max(1, min(cap, MAX_ELEMENTS))
    seen_hrefs: set[str] = set()
    candidates: list[ActionCandidate] = []
    for element in raw[:MAX_RAW_ELEMENTS]:
        if len(candidates) >= limit:
            break
        if element.password_input:
            # Only when a credential source exists. Password inputs bypass the
            # noise-name drop: a nameless password field is still the target.
            if not password_active:
                continue
            label = "" if is_noise_name(element.text) else element.text[:60]
            index = len(candidates) + 1
            candidates.append(
                ActionCandidate(
                    id=f"e{index}",
                    kind=ActionKind.TYPE,
                    description=(
                        f'input "{label or "password"}" '
                        "(fill with the configured password; it is never typed by a model)"
                    ),
                    fingerprint=element.fingerprint(),
                    stamp=element.attr,
                    action_key=f"type_e{index}",
                    enabled=element.enabled,
                )
            )
            continue
        if is_noise_name(element.text):
            continue
        if is_noise_href(element.href):
            continue
        if element.typeable and element.type_attr in {"password", "file"}:
            continue
        if element.href:
            key = element.href.split("#", 1)[0]
            if key in seen_hrefs:
                continue
            seen_hrefs.add(key)
        kind = _classify(element)
        if kind is None:
            continue
        index = len(candidates) + 1
        label = element.text[:60]
        options = tuple(label for _, label in element.options)
        option_indices = tuple(index for index, _ in element.options)
        candidates.append(
            ActionCandidate(
                id=f"e{index}",
                kind=kind,
                description=_describe(kind, element.role or element.tag, label, element),
                fingerprint=element.fingerprint(),
                stamp=element.attr,
                action_key=f"{kind.value}_e{index}",
                enabled=element.enabled,
                options=options,
                option_indices=option_indices,
                submit_via="click" if kind is ActionKind.SUBMIT else None,
            )
        )
        if kind is ActionKind.TYPE and element.enter_submittable and len(candidates) < limit:
            index = len(candidates) + 1
            candidates.append(
                ActionCandidate(
                    id=f"e{index}",
                    kind=ActionKind.SUBMIT,
                    description=f'{element.role or element.tag} "{label}" (submit the form now)',
                    fingerprint=element.fingerprint(),
                    stamp=element.attr,
                    action_key=f"submit_e{index}",
                    enabled=element.enabled,
                    submit_via="enter",
                )
            )
    return ActionSpace(
        candidates=tuple(candidates),
        truncated=len(candidates) >= limit,
        element_budget=limit,
    )
