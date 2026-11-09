"""Jev judgment transports.

`JevTransport` is the only seam between the browser loop and an inference
provider. Two implementations ship:

* :class:`TypeSafeTransport` adapts the official TypeSafe Python SDK.
* :class:`MockTransport` replays fixtures for fully offline tests and CI.

A transport returns *raw* answers. Validation happens in `judgment.py`, so an
adapter can never sneak an unvalidated value into an executable proposal.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .contracts import ErrorCode, TransportError

__all__ = [
    "JevTransport",
    "MockTransport",
    "ScriptedTransport",
    "TransportReply",
    "TypeSafeTransport",
    "as_transport_reply",
]


class TransportReply:
    """Raw provider reply: unvalidated answers plus accounting metadata."""

    __slots__ = ("answers", "input_tokens", "model", "output_tokens", "provider")

    def __init__(
        self,
        *,
        answers: Mapping[str, Any],
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.answers = answers
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def usage(self) -> tuple[int, int]:
        return (self.input_tokens, self.output_tokens)

    def __repr__(self) -> str:
        return (
            f"TransportReply(provider={self.provider!r}, model={self.model!r}, "
            f"answers={sorted(self.answers)!r}, input_tokens={self.input_tokens!r}, "
            f"output_tokens={self.output_tokens!r})"
        )


@runtime_checkable
class JevTransport(Protocol):
    """Provider-independent judgment call.

    Implementations must never touch the page. `deadline` is an absolute
    `time.monotonic()` value; a transport that cannot finish before it must
    raise rather than return a late answer.
    """

    name: str

    async def judge(
        self,
        *,
        state: object,
        questions: Mapping[str, object],
        deadline: float,
    ) -> TransportReply: ...


def as_transport_reply(reply: object, *, provider: str) -> TransportReply:
    """Normalize a duck-typed reply into :class:`TransportReply`.

    Accepts this module's class and the plain
    ``{answers, usage: {input_tokens, output_tokens}, model}`` shape, so a
    fixture or a host adapter can stay dependency-free.
    """
    if isinstance(reply, TransportReply):
        return reply
    answers: Any = getattr(reply, "answers", None)
    if answers is None and isinstance(reply, Mapping):
        answers = reply.get("answers")
    if not isinstance(answers, Mapping):
        raise TransportError(f"Jev provider {provider}: reply does not contain an answers object")
    model: Any = getattr(reply, "model", None)
    if model is None and isinstance(reply, Mapping):
        model = reply.get("model")
    usage: Any = getattr(reply, "usage", None)
    if usage is None and isinstance(reply, Mapping):
        usage = reply.get("usage")
    input_tokens: Any = 0
    output_tokens: Any = 0
    if usage is not None:
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        if isinstance(usage, Mapping):
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
        input_tokens = 0 if input_tokens is None else input_tokens
        output_tokens = 0 if output_tokens is None else output_tokens
    if not _is_int(input_tokens) or input_tokens < 0:
        raise TransportError(f"Jev provider {provider}: input_tokens must be a non-negative integer")
    if not _is_int(output_tokens) or output_tokens < 0:
        raise TransportError(f"Jev provider {provider}: output_tokens must be a non-negative integer")
    if not isinstance(model, str) or not model.strip():
        raise TransportError(f"Jev provider {provider}: a non-empty model id is required")
    return TransportReply(
        answers=dict(answers),
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class TypeSafeTransport:
    """Adapter over the official TypeSafe Python SDK.

    Connection settings are injected, never read from the browser layer. The
    SDK client is created lazily and reused for the transport's lifetime;
    SDK retries are disabled because the run loop owns the budget and must not
    silently spend more calls than `RunLimits` allows.
    """

    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    timeout: float | None = 30.0
    name: str = "typesafe"
    _client: Any = field(default=None, init=False, repr=False)

    def _build_client(self) -> Any:
        try:
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise TransportError("typesafe-sdk is not installed", code=ErrorCode.TRANSPORT_FAILED) from error
        kwargs: dict[str, Any] = {"retry": RetryPolicy(max_retries=0)}
        if self.api_key is not None:
            kwargs["api_key"] = self.api_key
        if self.model is not None:
            kwargs["model"] = self.model
        if self.base_url is not None:
            kwargs["base_url"] = self.base_url
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return AsyncTypeSafeClient(**kwargs)

    async def judge(
        self,
        *,
        state: object,
        questions: Mapping[str, object],
        deadline: float,
    ) -> TransportReply:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError(f"Jev provider {self.name}: deadline expired before the request")
        if self._client is None:
            self._client = self._build_client()
        try:
            response = await asyncio.wait_for(
                self._client.system_one(state=state, questions=dict(questions)),
                timeout=remaining,
            )
        except TimeoutError as error:
            raise TransportError(f"Jev provider {self.name}: request timed out") from error
        except TransportError:
            raise
        except Exception as error:
            raise TransportError(_summarize_provider_error(self.name, error)) from error
        return as_transport_reply(
            {
                "answers": _answers_as_mappings(response),
                "model": getattr(response, "model", "") or self.model or "",
                "usage": getattr(response, "usage", None),
            },
            provider=self.name,
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


def _answers_as_mappings(response: object) -> dict[str, Any]:
    answers = getattr(response, "answers", None)
    if not isinstance(answers, Mapping):
        raise TransportError("Jev provider reply does not contain answers")
    normalized: dict[str, Any] = {}
    for name, answer in answers.items():
        if isinstance(answer, Mapping):
            normalized[name] = dict(answer)
            continue
        dump = getattr(answer, "model_dump", None)
        if callable(dump):
            normalized[name] = dump()
            continue
        if hasattr(answer, "type"):
            payload = {"type": answer.type}
            for attribute in ("choice", "confidence", "probabilities", "noul", "score"):
                if hasattr(answer, attribute):
                    payload[attribute] = getattr(answer, attribute)
            normalized[name] = payload
            continue
        normalized[name] = answer
    return normalized


def _summarize_provider_error(provider: str, error: Exception) -> str:
    """Classify a provider failure without echoing response bodies.

    TypeSafe SDK errors may carry a response; only the HTTP status and the
    exception name are retained, matching the upstream rule that provider
    bodies never reach traces or the model.
    """
    status: Any = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "status", None)
    name = type(error).__name__
    if _is_int(status) and 100 <= status < 600:
        if status == 429:
            return f"Jev provider {provider}: rate limited (HTTP 429)"
        if status >= 500:
            return f"Jev provider {provider}: unavailable (HTTP {status})"
        return f"Jev provider {provider}: request failed (HTTP {status})"
    return f"Jev provider {provider}: request failed ({name})"


def _reply_from_answers(
    answers: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> TransportReply:
    return TransportReply(
        answers=answers,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@dataclass
class MockTransport:
    """Replays a scripted list of replies; used by every offline test.

    Each call consumes the next reply. Running past the script is an explicit
    failure, never a default answer.
    """

    replies: Sequence[Any]
    name: str = "mock"
    model: str = "jev-mock-1"
    input_tokens: int = 100
    output_tokens: int = 10
    delay_seconds: float = 0.0
    calls: list[tuple[object, Mapping[str, object]]] = field(default_factory=list)

    async def judge(
        self,
        *,
        state: object,
        questions: Mapping[str, object],
        deadline: float,
    ) -> TransportReply:
        self.calls.append((state, questions))
        if time.monotonic() >= deadline:
            raise TransportError(f"Jev provider {self.name}: deadline expired before the request")
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        index = len(self.calls) - 1
        if index >= len(self.replies):
            raise TransportError(f"Jev provider {self.name}: script exhausted after {len(self.replies)} replies")
        reply = self.replies[index]
        if callable(reply):
            reply = reply(state, questions)
        return as_transport_reply(
            {
                "answers": reply,
                "model": self.model,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            },
            provider=self.name,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


@dataclass
class ScriptedTransport:
    """Transport whose reply is produced by a callback, for stateful tests."""

    handler: Any
    name: str = "scripted"
    model: str = "jev-scripted-1"
    calls: list[tuple[object, Mapping[str, object]]] = field(default_factory=list)

    async def judge(
        self,
        *,
        state: object,
        questions: Mapping[str, object],
        deadline: float,
    ) -> TransportReply:
        self.calls.append((state, questions))
        if time.monotonic() >= deadline:
            raise TransportError(f"Jev provider {self.name}: deadline expired before the request")
        reply = self.handler(state, questions)
        if hasattr(reply, "__await__"):
            reply = await reply
        return as_transport_reply(reply, provider=self.name)

    @property
    def call_count(self) -> int:
        return len(self.calls)
