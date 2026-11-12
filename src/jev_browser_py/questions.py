"""Typed Jev question catalog.

Every judgment in one place, mirroring the upstream Jev Browser catalog
(`src/questions.ts` at 913d1a1): a fan-out of three independent questions over
the same state. The questions cannot see each other's answers, which is what
makes `goal_done` an honest cross-check on the `action` choice instead of a
rationalization of it.

Choice cardinality is bounded by the provider; the action space cap keeps the
catalog inside it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from typesafe_sdk import Choice, Noul

from .contracts import ActionSpace, PageSnapshot
from .redaction import redact_url_for_model, redact_urls_in_text

__all__ = [
    "CONTROL_ACTIONS",
    "ELEMENT_DESCRIPTION_MAX",
    "MAX_ELEMENTS",
    "SELECT_OPTION_MAX",
    "build_criteria",
    "select_option_question",
    "step_questions",
]

MAX_ELEMENTS = 240
"""Max elements offered per step. TypeSafe Choice supports 255 options; the
catalog reserves room for the four control actions below."""

SELECT_OPTION_MAX = 200
ELEMENT_DESCRIPTION_MAX = 300

CONTROL_ACTIONS: Mapping[str, str] = {
    "scroll_down": "Scroll down one screen to reveal more of the page",
    "scroll_up": "Scroll up one screen",
    "back": "Go back to the previous page; this branch is wrong",
    "done": "The task is already complete; stop here",
}
"""Loop controls offered with every judgment, exactly like upstream."""


def _clip(text: str, limit: int = ELEMENT_DESCRIPTION_MAX) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def build_criteria(action_space: ActionSpace) -> dict[str, str]:
    """Choice criteria for one step: element actions plus loop controls.

    Keys are the action keys the executor understands (`click_e3`, ...), so a
    proposal can be validated against them without a second mapping.
    """
    criteria: dict[str, str] = {
        candidate.action_key: _clip(redact_urls_in_text(candidate.description))
        for candidate in action_space.candidates[:MAX_ELEMENTS]
    }
    criteria.update(CONTROL_ACTIONS)
    return criteria


def step_questions(criteria: Mapping[str, str]) -> dict[str, Choice | Noul]:
    """The primary Jev call for each step: three independent judgments."""
    if not criteria:
        raise ValueError("criteria must not be empty")
    if len(criteria) > 255:
        raise ValueError("criteria exceeds the Choice option limit")
    return {
        "action": Choice(
            instructions="Which single action best advances the task on the current page?",
            criteria=dict(criteria),
        ),
        "goal_done": Noul(
            instructions=("The task's goal has been achieved: the current page and history show the sought outcome"),
            criteria={
                "true": "The page being viewed is the sought destination or shows the sought information",
                "false": "The goal is not yet achieved",
            },
        ),
        "stuck": Noul(
            instructions=("The actions so far are not making progress toward the task (repeats, loops, or no change)"),
            criteria={
                "true": "Recent actions repeat or nothing changes; a different strategy is needed",
                "false": "Progress is visible or the first steps are still reasonable",
            },
        ),
    }


def select_option_question(element_description: str, options: Sequence[str]) -> Choice:
    """Second-stage question for a native `<select>`.

    Asked only when a step picked `select_eN`. Option labels are the defined
    set; the model picks one, and selection happens by live DOM index so a
    scrubbed or truncated label can never become the selection key.
    """
    if not options:
        raise ValueError("a select question needs at least one option")
    if len(options) > SELECT_OPTION_MAX:
        raise ValueError("too many options for one select question")
    criteria = {f"o{index}": _clip(redact_urls_in_text(label), 80) or "(empty)" for index, label in enumerate(options)}
    safe_description = redact_urls_in_text(element_description)
    return Choice(
        instructions=(
            f'Which option should be selected in the dropdown "{_clip(safe_description, 80)}", '
            "given the task and the page?"
        ),
        criteria=criteria,
    )


def judgment_state(
    *,
    task: str,
    snapshot: PageSnapshot,
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The model-facing state for one judgment.

    Bounded by construction: the excerpt and per-element descriptions are
    already capped, and the history slice keeps the prompt from growing with
    the run length.
    """
    safe_history = [
        {key: redact_urls_in_text(value) if isinstance(value, str) else value for key, value in entry.items()}
        for entry in history[-8:]
    ]
    return {
        "task": redact_urls_in_text(task),
        "current_page": {
            "url": redact_url_for_model(snapshot.url),
            "title": redact_urls_in_text(snapshot.title),
        },
        "page_text_excerpt": redact_urls_in_text(snapshot.excerpt),
        "interactive_elements": [
            {"id": candidate.id, "description": redact_urls_in_text(candidate.description)}
            for candidate in snapshot.action_space.candidates
        ],
        "element_list_truncated": snapshot.action_space.truncated,
        "no_interactive_elements": len(snapshot.action_space) == 0,
        "history": safe_history,
    }
