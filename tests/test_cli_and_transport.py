"""CLI surface tests: parsing, refusal, and selector specs. No network."""

from __future__ import annotations

import pytest

from jev_browser_py.cli import _parse_selector_spec, build_parser, main


def test_parser_requires_a_goal_and_url() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["only a goal"])


def test_parser_defaults_are_conservative() -> None:
    args = build_parser().parse_args(["goal", "--url", "https://example.com/"])
    assert args.allow_origin == []
    assert args.allow_external_write is False
    assert args.allow_ambiguous is False
    assert args.allow_private is False
    assert args.headless is True
    assert args.max_steps == 12
    assert args.out is None


def test_headless_can_be_disabled_explicitly() -> None:
    args = build_parser().parse_args(["g", "--url", "https://x/", "--headed"])
    assert args.headless is False


def test_refuses_without_an_allowed_origin(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["find something", "--url", "https://example.com/"])
    assert exit_code == 2
    assert "--allow-origin" in capsys.readouterr().err


def test_refuses_without_an_api_key(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    exit_code = main(["find something", "--url", "https://example.com/", "--allow-origin", "https://example.com"])
    assert exit_code == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_selector_spec_parsing() -> None:
    spec = _parse_selector_spec("title=title")
    assert spec.name == "title"
    assert spec.selector == "title"


def test_selector_spec_requires_a_css_part() -> None:
    with pytest.raises(SystemExit):
        _parse_selector_spec("title")


def test_extract_flag_is_repeatable() -> None:
    args = build_parser().parse_args(["g", "--url", "https://x/", "--extract", "a=a", "--extract", "b=b"])
    assert args.extract == ["a=a", "b=b"]


# ── transport normalization ─────────────────────────────────────────────────


def test_typesafe_answer_mapping_handles_model_objects() -> None:
    from jev_browser_py.transport import _answers_as_mappings

    class FakeAnswer:
        type = "choice"
        choice = "click_e1"
        confidence = 0.9

        def __init__(self) -> None:
            self.probabilities = {"click_e1": 1.0}

    normalised = _answers_as_mappings(type("R", (), {"answers": {"action": FakeAnswer()}})())
    assert normalised["action"]["choice"] == "click_e1"
    assert normalised["action"]["type"] == "choice"


def test_provider_error_summary_never_includes_a_body() -> None:
    from jev_browser_py.transport import _summarize_provider_error

    class RateLimited(Exception):
        status = 429

    message = _summarize_provider_error("typesafe", RateLimited("SECRET BODY sk-live-123"))
    assert "SECRET" not in message
    assert "429" in message


def test_provider_error_summary_classifies_server_errors() -> None:
    from jev_browser_py.transport import _summarize_provider_error

    class Unavailable(Exception):
        status = 503

    assert "unavailable" in _summarize_provider_error("typesafe", Unavailable("x"))


def test_provider_error_summary_unknown_shape() -> None:
    from jev_browser_py.transport import _summarize_provider_error

    message = _summarize_provider_error("typesafe", ValueError("nope"))
    assert "ValueError" in message


async def test_typesafe_transport_requires_a_deadline_ahead() -> None:
    import time

    from jev_browser_py.contracts import TransportError
    from jev_browser_py.transport import TypeSafeTransport

    transport = TypeSafeTransport(api_key="test-key-not-used")
    with pytest.raises(TransportError, match="deadline expired"):
        await transport.judge(state={}, questions={}, deadline=time.monotonic() - 1)


async def test_typesafe_transport_normalizes_a_mock_sdk_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import time

    from jev_browser_py.transport import TypeSafeTransport

    class FakeUsage:
        input_tokens = 42
        output_tokens = 7

    class FakeAnswer:
        def model_dump(self) -> dict[str, object]:
            return {"type": "noul", "noul": 0.9}

    class FakeResponse:
        model = "jev-test"
        usage = FakeUsage()
        answers: dict[str, object]

        def __init__(self) -> None:
            self.answers = {"goal_done": FakeAnswer()}

    class FakeClient:
        async def system_one(self, *, state: object, questions: object) -> FakeResponse:
            del state, questions
            return FakeResponse()

    transport = TypeSafeTransport(api_key="test-key-not-used")
    transport._client = FakeClient()
    reply = await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)
    assert reply.input_tokens == 42
    assert reply.model == "jev-test"
    assert reply.answers["goal_done"]["noul"] == 0.9


async def test_typesafe_transport_surfaces_a_client_exception() -> None:
    import time

    from jev_browser_py.contracts import TransportError
    from jev_browser_py.transport import TypeSafeTransport

    class FailingClient:
        async def system_one(self, *, state: object, questions: object) -> object:
            del state, questions
            raise RuntimeError("connection reset")

    transport = TypeSafeTransport(api_key="test-key-not-used")
    transport._client = FailingClient()
    with pytest.raises(TransportError, match="request failed"):
        await transport.judge(state={}, questions={}, deadline=time.monotonic() + 5)


def test_transport_reply_repr_shows_only_answer_names() -> None:
    """The repr names the answers but never echoes their values."""
    from jev_browser_py.transport import TransportReply

    reply = TransportReply(
        answers={"action": {"choice": "click_e1"}},
        model="m",
        provider="p",
        input_tokens=1,
        output_tokens=2,
    )
    text = repr(reply)
    assert "action" in text
    assert "click_e1" not in text
    assert reply.usage == (1, 2)
