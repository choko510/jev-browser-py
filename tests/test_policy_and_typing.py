"""Policy matrix, text generation, and telemetry tests."""

from __future__ import annotations

import pytest

from conftest import make_snapshot
from jev_browser_py.action_space import RawElement, build_action_space
from jev_browser_py.contracts import (
    ActionKind,
    ErrorCode,
    Judgment,
    PolicyDeniedError,
    SideEffectClass,
    Usage,
)
from jev_browser_py.judgment import parse_proposal, side_effect_of
from jev_browser_py.policy import (
    ActionPolicy,
    AllowAllReadPolicy,
    default_policy,
    denial_error,
    describe_denial,
)


def _proposal(action_space, action: str, *, goal: str = ""):  # type: ignore[no-untyped-def]
    judgment = Judgment(
        action=action,
        goal_done=0.0,
        stuck=0.0,
        probabilities={action: 1.0},
        usage=Usage(),
        model="m",
        provider="fixture",
        snapshot_id="snap-1",
        goal_revision=0,
    )
    return parse_proposal(judgment, action_space=action_space, goal=goal)


def _space(text: str, *, kind: str = "button", submit: bool = False):  # type: ignore[no-untyped-def]
    return build_action_space(
        [
            RawElement(
                attr="j1",
                tag=kind,
                role=kind,
                text=text,
                href="/docs" if kind == "a" else "",
                type_attr="submit" if submit else "",
                clickable=True,
                submit_control=submit,
            )
        ]
    )


def test_read_click_is_allowed() -> None:
    space = _space("Documentation", kind="a")
    snapshot = make_snapshot(action_space=space)
    assert default_policy().decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot).allowed


def test_ambiguous_labels_are_denied() -> None:
    space = _space("Sign in")
    snapshot = make_snapshot(action_space=space)
    decision = default_policy().decide(proposal=_proposal(space, "click_e1", goal="log in"), snapshot=snapshot)
    assert decision.allowed is False
    assert decision.side_effect is SideEffectClass.AMBIGUOUS


def test_ambiguous_can_be_opened_explicitly() -> None:
    space = _space("Sign in")
    snapshot = make_snapshot(action_space=space)
    decision = default_policy(allow_ambiguous=True).decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot)
    assert decision.allowed is True


def test_search_submit_for_post_form_requires_external_write_permission() -> None:
    space = build_action_space(
        [
            RawElement(
                attr="j1",
                tag="input",
                role="searchbox",
                text="Search",
                type_attr="search",
                typeable=True,
                search_field=True,
                enter_submittable=True,
                form_method="post",
            )
        ]
    )
    submit = next(candidate for candidate in space.candidates if candidate.kind is ActionKind.SUBMIT)
    assert side_effect_of(submit.action_key, submit) is SideEffectClass.EXTERNAL_WRITE
    decision = default_policy().decide(
        proposal=_proposal(space, submit.action_key),
        snapshot=make_snapshot(action_space=space),
    )
    assert decision.allowed is False


def test_get_search_submit_remains_a_read_action() -> None:
    space = build_action_space(
        [
            RawElement(
                attr="j1",
                tag="input",
                role="searchbox",
                text="Search",
                type_attr="search",
                typeable=True,
                search_field=True,
                enter_submittable=True,
                form_method="get",
            )
        ]
    )
    submit = next(candidate for candidate in space.candidates if candidate.kind is ActionKind.SUBMIT)
    assert side_effect_of(submit.action_key, submit) is SideEffectClass.READ
    decision = default_policy().decide(
        proposal=_proposal(space, submit.action_key),
        snapshot=make_snapshot(action_space=space),
    )
    assert decision.allowed is True


def test_external_write_can_be_opened_explicitly() -> None:
    space = _space("Place order", submit=True)
    snapshot = make_snapshot(action_space=space)
    decision = default_policy(allow_external_write=True).decide(
        proposal=_proposal(space, "submit_e1"), snapshot=snapshot
    )
    assert decision.allowed is True


def test_unrecognized_state_changing_button_is_not_assumed_read_only() -> None:
    space = _space("Save settings")
    snapshot = make_snapshot(action_space=space)
    proposal = _proposal(space, "click_e1")

    assert side_effect_of("click_e1", space.candidates[0]) is SideEffectClass.AMBIGUOUS
    decision = default_policy().decide(proposal=proposal, snapshot=snapshot)
    assert decision.allowed is False


def test_deny_actions_list_is_honoured() -> None:
    space = _space("Documentation")
    snapshot = make_snapshot(action_space=space)
    policy = ActionPolicy(deny_actions=frozenset({"click_e1"}))
    decision = policy.decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot)
    assert decision.allowed is False
    assert "explicitly denied" in decision.reason


def test_deny_kinds_list_is_honoured() -> None:
    space = _space("Documentation")
    snapshot = make_snapshot(action_space=space)
    policy = ActionPolicy(deny_kinds=frozenset({ActionKind.CLICK}))
    assert policy.decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot).allowed is False


def test_a_mismatched_snapshot_is_denied() -> None:
    space = _space("Documentation")
    snapshot = make_snapshot(action_space=space, snapshot_id="other")
    decision = default_policy().decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot)
    assert decision.allowed is False
    assert "different snapshot" in decision.reason


def test_allow_all_read_policy_still_denies_writes() -> None:
    space = _space("Delete", submit=True)
    snapshot = make_snapshot(action_space=space)
    assert AllowAllReadPolicy().decide(proposal=_proposal(space, "submit_e1"), snapshot=snapshot).allowed is False


def test_denial_error_and_description_never_leak_secrets() -> None:
    space = _space("Documentation")
    snapshot = make_snapshot(action_space=space)
    decision = default_policy().decide(proposal=_proposal(space, "click_e1"), snapshot=snapshot)
    error = denial_error(decision) if not decision.allowed else None
    if error is not None:
        assert isinstance(error, PolicyDeniedError)
        assert error.code is ErrorCode.POLICY_DENIED
    assert describe_denial(None) == "no element target"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Documentation", SideEffectClass.READ),
        ("Buy now", SideEffectClass.EXTERNAL_WRITE),
        ("Continue", SideEffectClass.AMBIGUOUS),
        ("Accept cookies", SideEffectClass.AMBIGUOUS),
    ],
)
def test_side_effect_classification(text: str, expected: SideEffectClass) -> None:
    space = _space(text, kind="a" if expected is SideEffectClass.READ else "button")
    candidate = space.candidates[0]
    assert side_effect_of("click_e1", candidate) is expected


def test_scroll_and_back_are_read_only_controls() -> None:
    assert side_effect_of("scroll_down", None) is SideEffectClass.READ
    assert side_effect_of("back", None) is SideEffectClass.READ
    assert side_effect_of("mystery", None) is SideEffectClass.AMBIGUOUS


def test_authorize_is_awaitable_for_host_adapters() -> None:
    import asyncio

    space = _space("Documentation", kind="a")
    snapshot = make_snapshot(action_space=space)
    decision = asyncio.run(default_policy().authorize(proposal=_proposal(space, "click_e1"), snapshot=snapshot))
    assert decision.allowed is True


# ── typing ───────────────────────────────────────────────────────────────────


def test_heuristic_query_prefers_a_quoted_phrase() -> None:
    from jev_browser_py.typing import heuristic_query

    assert heuristic_query('search for "annual reports" and stop') == "annual reports"
    assert heuristic_query("look up “The Who” and stop") == "The Who"


def test_heuristic_query_uses_called_named_target() -> None:
    from jev_browser_py.typing import heuristic_query

    assert heuristic_query("find the book called The Quiet Garden") == "The Quiet Garden"


def test_heuristic_query_strips_stop_words() -> None:
    from jev_browser_py.typing import heuristic_query

    query = heuristic_query("go to the page about rust async runtime")
    assert "the" not in query.split()
    assert "rust" in query


def test_validate_generated_text_rejects_bad_values() -> None:
    from jev_browser_py.typing import TextGenerationError, validate_generated_text

    assert validate_generated_text("hello", via="x").text == "hello"
    with pytest.raises(TextGenerationError, match="no text"):
        validate_generated_text("   ", via="x")
    with pytest.raises(TextGenerationError, match="control"):
        validate_generated_text("a\x00b", via="x")
    with pytest.raises(TextGenerationError, match="URL"):
        validate_generated_text("javascript:alert(1)", via="x")
    with pytest.raises(TextGenerationError, match="characters"):
        validate_generated_text("x" * 5000, via="x")


def test_validate_generated_text_strips_quotes() -> None:
    from jev_browser_py.typing import validate_generated_text

    assert validate_generated_text('"quoted search"', via="x").text == "quoted search"


async def test_callable_generator_validates_output() -> None:
    from jev_browser_py.typing import CallableTextGenerator, TextGenerationError

    async def good(task: str, description: str, url: str) -> str:
        return "query text"

    async def bad(task: str, description: str, url: str) -> str:
        return ""

    result = await CallableTextGenerator(good).generate(task="t", element_description="Search", url="https://x.test/")
    assert result.text == "query text"
    with pytest.raises(TextGenerationError):
        await CallableTextGenerator(bad).generate(task="t", element_description="Search", url="https://x.test/")


async def test_rule_based_generator_refuses_identity_fields() -> None:
    from jev_browser_py.typing import RuleBasedTextGenerator, TextGenerationError

    generator = RuleBasedTextGenerator()
    with pytest.raises(TextGenerationError, match="user-supplied"):
        await generator.generate(task="sign in", element_description="Email address", url="https://x.test/")
    result = await generator.generate(
        task='search for "annual reports"', element_description="Search", url="https://x.test/"
    )
    assert result.text == "annual reports"


def test_openai_compatible_generator_rejects_bad_configuration() -> None:
    from jev_browser_py.typing import OpenAICompatibleTextGenerator

    with pytest.raises(ValueError, match="absolute http"):
        OpenAICompatibleTextGenerator(base_url="ftp://x", model="m")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleTextGenerator(base_url="http://127.0.0.1:11434/v1", model=" ")


def test_sensitive_field_detection() -> None:
    from jev_browser_py.typing import is_sensitive_field

    assert is_sensitive_field("Password") is True
    assert is_sensitive_field("Credit card number") is True
    assert is_sensitive_field("Search the docs") is False


def test_text_digest_is_stable_and_short() -> None:
    from jev_browser_py.typing import text_digest

    assert text_digest("hello") == text_digest("hello")
    assert len(text_digest("hello")) == 16
    assert text_digest("hello") != text_digest("hellp")


# ── telemetry ────────────────────────────────────────────────────────────────


def test_telemetry_counts_and_cost() -> None:
    from jev_browser_py.telemetry import Telemetry

    telemetry = Telemetry()
    telemetry.record_judgment(input_tokens=1_000_000, output_tokens=5)
    assert telemetry.jev_calls == 1
    assert abs(telemetry.estimated_cost_usd - 0.042) < 1e-9
    assert telemetry.as_dict()["jev_calls"] == 1


def test_run_report_is_serializable_and_secret_free() -> None:
    import json

    from jev_browser_py.contracts import RunResult, RunStatus
    from jev_browser_py.telemetry import run_summary

    result = RunResult(
        status=RunStatus.SUCCESS,
        reason="verified",
        verified=True,
        steps=2,
        usage=Usage(input_tokens=100, output_tokens=10),
        jev_calls=2,
        elapsed_seconds=1.5,
        final_url="https://example.test/",
    )
    payload = run_summary(result).as_dict()
    text = json.dumps(payload)
    assert "sk-" not in text
    assert payload["verified"] is True
    assert payload["status"] == "success"


def test_trace_store_redacts_and_writes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jev_browser_py.contracts import RunResult, RunStatus
    from jev_browser_py.redaction import REDACTED, Redactor
    from jev_browser_py.telemetry import TraceStore

    store = TraceStore(tmp_path, redactor=Redactor(["super-secret-token"]))
    path = store.write_json({"note": "contains super-secret-token here", "ok": 1}, name="x.json")
    text = path.read_text(encoding="utf-8")
    assert "super-secret-token" not in text
    assert REDACTED in text
    result = RunResult(status=RunStatus.UNVERIFIED, reason="nope")
    report_path = store.write_report(result, name="run.json")
    assert report_path.exists()


def test_trace_store_refuses_unredactable_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jev_browser_py.redaction import REDACTED, Redactor
    from jev_browser_py.telemetry import TraceStore

    store = TraceStore(tmp_path, redactor=Redactor(["a-long-secret-value"]))
    path = store.write_json({"obj": object()}, name="bad.json")
    assert REDACTED in path.read_text(encoding="utf-8")
