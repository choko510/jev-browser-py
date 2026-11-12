"""Typed text generation for fill/search actions.

Jev chooses *which* field; it never decides *what* to type. That is a separate
concern with its own validation:

* The generated text is length-capped, type-checked, and stripped.
* A generator failure types nothing for an ordinary field (never a guess).
* A search field may fall back to a deterministic keyword heuristic, and the
  run records a structured warning rather than silently degrading.
* The generator is never allowed to chain into a submit.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .contracts import ErrorCode, JevBrowserError
from .transport import CostEstimator

__all__ = [
    "TYPING_WARNING_MESSAGE_MAX",
    "CallableTextGenerator",
    "GeneratedText",
    "OpenAICompatibleTextGenerator",
    "RuleBasedTextGenerator",
    "TextGenerationError",
    "TextGenerator",
    "heuristic_query",
    "is_sensitive_field",
    "text_digest",
    "validate_generated_text",
]

TYPING_WARNING_MESSAGE_MAX = 200
TEXT_MAX = 400
GENERATOR_TEXT_MAX = 400

_STOP_WORDS = frozenset(
    {
        "search",
        "wikipedia",
        "the",
        "a",
        "an",
        "article",
        "about",
        "for",
        "find",
        "stop",
        "when",
        "you",
        "are",
        "on",
        "it",
        "and",
        "to",
        "of",
        "called",
        "page",
        "site",
        "website",
        "navigate",
        "go",
        "open",
        "that",
        "this",
    }
)

_URL_SHAPE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class TextGenerationError(JevBrowserError):
    code = ErrorCode.TRANSPORT_FAILED


@dataclass(frozen=True, slots=True)
class GeneratedText:
    """A validated string plus how it was produced."""

    text: str
    via: str
    degraded: bool = False
    warning: str = ""


@runtime_checkable
class TextGenerator(CostEstimator, Protocol):
    """Produces the text for one field, given the task and the field."""

    provider: str

    async def generate(
        self, *, task: str, element_description: str, url: str, deadline: float | None = None
    ) -> GeneratedText: ...


@dataclass
class RuleBasedTextGenerator(CostEstimator):
    """Deterministic, offline generator used by tests and as a safe default.

    It never invents personal data: it derives a short query from the task
    text, and for a field that looks like an email/username it refuses rather
    than fabricating an identity.
    """

    provider: str = "rule-based"
    redactor_patterns: tuple[str, ...] = field(default_factory=tuple)

    def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float:
        del request
        return 0.0

    async def generate(
        self, *, task: str, element_description: str, url: str, deadline: float | None = None
    ) -> GeneratedText:
        del deadline
        lowered = element_description.lower()
        if any(word in lowered for word in ("password", "email", "e-mail", "username", "login", "phone")):
            raise TextGenerationError("this field needs a user-supplied value; the generator will not invent one")
        query = heuristic_query(task)
        if not query:
            raise TextGenerationError("no usable query could be derived from the task")
        return GeneratedText(text=query, via=self.provider, degraded=True, warning="rule-based text")


@dataclass
class CallableTextGenerator:
    """Wraps an async callable, validating its output like any other provider."""

    func: Callable[[str, str, str], Awaitable[str]]
    provider: str = "callable"
    max_length: int = GENERATOR_TEXT_MAX
    max_cost_per_call_usd: float | None = None

    def __post_init__(self) -> None:
        if self.max_cost_per_call_usd is not None and (
            not math.isfinite(self.max_cost_per_call_usd) or self.max_cost_per_call_usd < 0
        ):
            raise ValueError("max_cost_per_call_usd must be finite and non-negative")

    def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float | None:
        del request
        return self.max_cost_per_call_usd

    async def generate(
        self, *, task: str, element_description: str, url: str, deadline: float | None = None
    ) -> GeneratedText:
        del deadline
        raw = await self.func(task, element_description, url)
        return validate_generated_text(raw, via=self.provider, max_length=self.max_length)


@dataclass
class OpenAICompatibleTextGenerator:
    """Generates field text through any OpenAI-compatible chat endpoint.

    Deliberately provider-agnostic: a local Ollama/LM Studio endpoint works the
    same as a hosted API. The endpoint and key are injected by the caller and
    are never read from the page. Failures raise; they never fabricate text.
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 20.0
    provider: str = "openai-compatible"
    max_length: int = GENERATOR_TEXT_MAX
    max_cost_per_call_usd: float | None = None

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.max_cost_per_call_usd is not None and (
            not math.isfinite(self.max_cost_per_call_usd) or self.max_cost_per_call_usd < 0
        ):
            raise ValueError("max_cost_per_call_usd must be finite and non-negative")

    def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float | None:
        del request
        return self.max_cost_per_call_usd

    async def generate(
        self, *, task: str, element_description: str, url: str, deadline: float | None = None
    ) -> GeneratedText:
        timeout = self.timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TextGenerationError("the run deadline expired before the typing request")
            timeout = min(timeout, remaining)
        prompt = (
            f'A browser agent is performing this task: "{task}". '
            f"It must type into the {element_description} on {url}. "
            "Reply with ONLY the exact text to type (for a search box: a short search query; "
            "no quotes, no explanation)."
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 64,
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            body = await _post_json(
                f"{self.base_url.rstrip('/')}/chat/completions",
                payload,
                headers,
                timeout,
            )
        except Exception as error:
            raise TextGenerationError(f"typing provider {self.provider}: request failed") from error
        text = _first_message_text(body)
        return validate_generated_text(text, via=self.provider, max_length=self.max_length)


async def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], limit_seconds: float) -> Any:
    """Minimal POST helper; uses httpx if present, else urllib in a thread."""
    try:
        import httpx
    except ImportError:
        import asyncio
        import json
        import urllib.parse
        import urllib.request

        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise TextGenerationError("the typing endpoint must be an http(s) URL") from None

        def _blocking() -> Any:
            request = urllib.request.Request(  # noqa: S310 - scheme checked above
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=limit_seconds) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(_blocking)
    async with httpx.AsyncClient(timeout=limit_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def _first_message_text(body: Any) -> str:
    try:
        choices = body["choices"]
        message = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise TextGenerationError("the typing provider returned an unexpected response") from error
    if not isinstance(message, str):
        raise TextGenerationError("the typing provider returned no text")
    return message


def validate_generated_text(raw: str, *, via: str, max_length: int = GENERATOR_TEXT_MAX) -> GeneratedText:
    """Validate a generated string; rejects empties, control chars, and URLs."""
    if not isinstance(raw, str):
        raise TextGenerationError("generated text must be a string")
    text = raw.strip().strip("\"'").strip()
    if not text:
        raise TextGenerationError("the generator returned no text")
    if len(text) > max_length:
        raise TextGenerationError(f"the generator returned {len(text)} characters, over the {max_length} limit")
    if any(ord(character) < 32 and character not in "\t" for character in text):
        raise TextGenerationError("generated text contains control characters")
    if _URL_SHAPE.match(text):
        raise TextGenerationError("generated text looks like a URL scheme and is refused")
    return GeneratedText(text=text, via=via)


def heuristic_query(task: str) -> str:
    """Deterministic search-query fallback, ported from upstream `heuristicQuery`.

    Explicit targets keep meaningful words and punctuation: a quoted phrase
    wins; "called X" wins; otherwise stop words are removed.
    """
    quoted = re.search(r'"([^"]+)"|“([^”]+)”', task)
    if quoted:
        return (quoted.group(1) or quoted.group(2) or "").strip()
    goal = re.split(r"\s+(?:and|then)\s+stop\b", task, maxsplit=1, flags=re.IGNORECASE)[0]
    named = re.search(r"\bcalled\s+(.+)", task, flags=re.IGNORECASE)
    if named:
        return named.group(1).strip()
    words = [word for word in re.sub(r"[^a-z0-9\s-]", " ", goal.lower()).split() if word and word not in _STOP_WORDS]
    return " ".join(words[:6])


def text_digest(text: str) -> str:
    """Digest of typed text for traces; the text itself is never logged."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_sensitive_field(element_description: str) -> bool:
    lowered = element_description.lower()
    return any(
        word in lowered for word in ("password", "passcode", "pin", "credit card", "cvv", "ssn", "social security")
    )
