"""Regressions for the follow-up audit: Context policy ownership, WebSocket
message gating, masked-URL dedupe identity, and lock-wait accounting.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import make_snapshot, sample_action_space, step_reply
from fakes import FakeExecutor, FakePage, FakeSnapshotter
from jev_browser_py.action_space import RawElement, build_action_space, raw_from_mapping
from jev_browser_py.contracts import ErrorCode, NetworkGuardError, RunLimits, RunStatus
from jev_browser_py.runtime import run
from jev_browser_py.session import BrowserOwner, BrowserSession, NetworkPolicy
from jev_browser_py.snapshot import Snapshotter


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("jev_browser_py.session.socket.getaddrinfo", resolve)


class _RecordingContext:
    def __init__(self) -> None:
        self.http_handler: object | None = None
        self.websocket_handler: object | None = None
        self.pages: list[object] = []

    async def route(self, pattern: str, handler: object) -> None:
        assert pattern == "**/*"
        self.http_handler = handler

    async def route_web_socket(self, pattern: str, handler: object) -> None:
        assert pattern == "**/*"
        self.websocket_handler = handler


class _HttpRoute:
    def __init__(self, url: str, *, method: str = "GET", page: object | None = None) -> None:
        frame = SimpleNamespace(page=page) if page is not None else None
        self.request = SimpleNamespace(url=url, method=method, frame=frame)
        self.result = ""

    async def abort(self) -> None:
        self.result = "abort"

    async def fetch(self, *, max_redirects: int = 0) -> object:
        assert max_redirects == 0
        return SimpleNamespace(status=200, headers={})

    async def fulfill(self, *, response: object) -> None:
        self.result = "fulfill"


def _session(page: FakePage, context: _RecordingContext, policy: NetworkPolicy) -> BrowserSession:
    owner = BrowserOwner(
        browser=None,
        context=context,
        page=page,
        owns_browser=False,
        owns_context=False,
        owns_page=False,
    )
    return BrowserSession(page=page, owner=owner, network=policy)


async def test_a_context_enforces_one_policy_for_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    context = _RecordingContext()
    first_page = FakePage(url="https://a.test/")
    second_page = FakePage(url="https://b.test/")
    first = _session(first_page, context, NetworkPolicy(allowed_origins=("https://a.test",)))
    second = _session(second_page, context, NetworkPolicy(allowed_origins=("https://b.test",)))

    await first.install_guard()

    with pytest.raises(NetworkGuardError, match="already enforces a different network policy"):
        await second.install_guard()

    # The installed route keeps the first policy's decision: a second Page's
    # session cannot widen what the Context already allows.
    handler = context.http_handler
    assert callable(handler)
    b_request = _HttpRoute("https://b.test/api", page=second_page)
    await handler(b_request)
    assert b_request.result == "abort"

    a_request = _HttpRoute("https://a.test/api", page=second_page)
    await handler(a_request)
    assert a_request.result == "fulfill"


async def test_the_same_policy_object_may_guard_two_pages_in_one_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    context = _RecordingContext()
    policy = NetworkPolicy(allowed_origins=("https://a.test",))
    first = _session(FakePage(url="https://a.test/one"), context, policy)
    second = _session(FakePage(url="https://a.test/two"), context, policy)

    await first.install_guard()
    await second.install_guard()


async def test_a_failed_route_installation_releases_the_context_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)

    class FailingContext(_RecordingContext):
        fail = True

        async def route(self, pattern: str, handler: object) -> None:
            if self.fail:
                raise RuntimeError("route installation failed")
            await super().route(pattern, handler)

    context = FailingContext()
    policy = NetworkPolicy(allowed_origins=("https://a.test",))
    page = FakePage(url="https://a.test/")
    first = _session(page, context, policy)

    with pytest.raises(NetworkGuardError, match="could not be installed"):
        await first.install_guard()

    context.fail = False
    second = _session(FakePage(url="https://a.test/two"), context, policy)
    await second.install_guard()


class _ServerRoute:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []

    def send(self, message: str | bytes) -> None:
        self.sent.append(message)


class _WebSocketRoute:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.server = _ServerRoute()
        self.message_handler: object | None = None

    async def close(self, *, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True

    def connect_to_server(self) -> _ServerRoute:
        return self.server

    def on_message(self, handler: object) -> None:
        self.message_handler = handler


async def test_websocket_messages_are_dropped_without_an_authorized_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    context = _RecordingContext()
    page = FakePage(url="https://allowed.example/")
    session = _session(
        page,
        context,
        NetworkPolicy(allowed_origins=("https://allowed.example",), allow_unsafe_methods=True),
    )
    await session.install_guard()

    socket_route = _WebSocketRoute("wss://allowed.example/socket")
    handler = context.websocket_handler
    assert callable(handler)
    await handler(socket_route)

    assert socket_route.closed is False
    forward = socket_route.message_handler
    assert callable(forward)

    forward("read-only probe")
    assert socket_route.server.sent == []

    with session.network.authorize_unsafe_requests(page):
        forward("authorized write")
    assert socket_route.server.sent == ["authorized write"]

    forward("after the scope")
    assert socket_route.server.sent == ["authorized write"]


async def test_websocket_messages_never_forward_when_unsafe_methods_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    context = _RecordingContext()
    page = FakePage(url="https://allowed.example/")
    session = _session(page, context, NetworkPolicy(allowed_origins=("https://allowed.example",)))
    await session.install_guard()

    socket_route = _WebSocketRoute("wss://allowed.example/socket")
    handler = context.websocket_handler
    assert callable(handler)
    await handler(socket_route)
    forward = socket_route.message_handler
    assert callable(forward)
    forward("anything")
    assert socket_route.server.sent == []
    assert session.network.websocket_send_allowed() is False


def test_distinct_raw_urls_that_mask_identically_survive_dedupe() -> None:
    raw = [
        raw_from_mapping(
            {
                "attr": "j1",
                "tag": "a",
                "role": "a",
                "text": "First result",
                "href": "https://example.test/item?id=first-secret",
                "clickable": True,
            }
        ),
        raw_from_mapping(
            {
                "attr": "j2",
                "tag": "a",
                "role": "a",
                "text": "Second result",
                "href": "https://example.test/item?id=second-secret",
                "clickable": True,
            }
        ),
    ]
    snapshotter = Snapshotter(FakePage())
    masked = [snapshotter._redact_element(element) for element in raw]
    # Both hrefs mask to the same model-facing string; identity must differ.
    assert masked[0].href == masked[1].href
    assert masked[0].href_identity != masked[1].href_identity

    space = build_action_space(masked)

    assert [candidate.id for candidate in space.candidates] == ["e1", "e2"]
    model_text = " ".join(candidate.description for candidate in space.candidates)
    assert "first-secret" not in model_text
    assert "second-secret" not in model_text


def test_duplicate_raw_destinations_still_collapse_including_fragments() -> None:
    space = build_action_space(
        [
            raw_from_mapping(
                {"attr": "j1", "tag": "a", "role": "a", "text": "Docs", "href": "/docs", "clickable": True}
            ),
            raw_from_mapping(
                {"attr": "j2", "tag": "a", "role": "a", "text": "Install", "href": "/docs#install", "clickable": True}
            ),
            raw_from_mapping(
                {
                    "attr": "j3",
                    "tag": "a",
                    "role": "a",
                    "text": "Pricing",
                    "href": "https://example.test/pricing?plan=pro",
                    "clickable": True,
                }
            ),
        ]
    )

    assert len(space.candidates) == 2


def test_snapshot_keeps_raw_identity_private_and_masks_the_model_href() -> None:
    snapshotter = Snapshotter(FakePage())
    element = RawElement(
        attr="j1",
        tag="a",
        role="a",
        text="Open",
        href="https://example.test/account/abcdefghijklmnopqrstuvwxyz012345?token=leak",
        clickable=True,
    )

    safe = snapshotter._redact_element(element)

    assert safe.href_identity != ""
    assert "leak" not in safe.href
    assert "abcdefghijklmnopqrstuvwxyz012345" not in safe.href
    assert safe.href_identity != safe.href


async def test_run_deadline_includes_the_page_lock_wait() -> None:
    page = FakePage()
    snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    snapshotter.page = page  # type: ignore[attr-defined]
    executor = FakeExecutor()
    executor.page = page  # type: ignore[attr-defined]
    from jev_browser_py.session import _page_action_lock

    lock = _page_action_lock(page)
    await lock.acquire()
    try:
        result = await run(
            "click",
            transport=_zero_cost_transport("scroll_down"),
            snapshotter=snapshotter,
            executor=executor,
            limits=RunLimits(max_steps=2, max_seconds=0.05),
        )
    finally:
        lock.release()

    assert result.status is RunStatus.TIMEOUT
    assert result.error_code is ErrorCode.TIMEOUT
    assert snapshotter.observed == []
    assert executor.action_count == 0


async def test_a_later_run_cannot_touch_a_page_until_the_abandoned_operation_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jev_browser_py.runtime.DEADLINE_SETTLE_SECONDS", 0.05)
    release = asyncio.Event()

    class StubbornExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_one(self, *, proposal: object, snapshot: object, **kwargs: object) -> object:
            del proposal, snapshot, kwargs
            self.calls += 1
            while not release.is_set():
                try:
                    await asyncio.wait_for(release.wait(), timeout=0.01)
                except (TimeoutError, asyncio.CancelledError):
                    continue
            return SimpleNamespace(state=SimpleNamespace(value="acted"), detail="acted", error_code=None)

    page = FakePage()
    stubborn = StubbornExecutor()
    stubborn.page = page  # type: ignore[attr-defined]
    snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    snapshotter.page = page  # type: ignore[attr-defined]
    snapshot = make_snapshot(action_space=sample_action_space())

    first = await run(
        "scroll",
        transport=_zero_cost_transport("scroll_down"),
        snapshotter=snapshotter,
        executor=stubborn,
        limits=RunLimits(max_steps=1, max_seconds=0.05),
    )
    assert first.status is RunStatus.TIMEOUT
    assert stubborn.calls == 1

    second_snapshotter = FakeSnapshotter([snapshot])
    second_snapshotter.page = page  # type: ignore[attr-defined]
    second_executor = FakeExecutor()
    second_executor.page = page  # type: ignore[attr-defined]
    second = await run(
        "scroll",
        transport=_zero_cost_transport("scroll_down"),
        snapshotter=second_snapshotter,
        executor=second_executor,
        limits=RunLimits(max_steps=1, max_seconds=0.05),
    )

    assert second.status is RunStatus.BLOCKED
    assert second.error_code is ErrorCode.UNKNOWN_EFFECT
    assert second_snapshotter.observed == []
    assert second_executor.action_count == 0

    release.set()
    await asyncio.sleep(0.05)

    third_snapshotter = FakeSnapshotter([snapshot])
    third_snapshotter.page = page  # type: ignore[attr-defined]
    third_executor = FakeExecutor()
    third_executor.page = page  # type: ignore[attr-defined]
    third = await run(
        "scroll",
        transport=_zero_cost_transport("scroll_down"),
        snapshotter=third_snapshotter,
        executor=third_executor,
        limits=RunLimits(max_steps=1, max_seconds=5),
    )

    assert third.status is not RunStatus.BLOCKED
    assert third_executor.action_count == 1


def _zero_cost_transport(action: str) -> object:
    from jev_browser_py.transport import MockTransport

    return MockTransport([step_reply(sample_action_space(), action)])


async def test_a_later_page_cannot_borrow_another_pages_websocket_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket route has no Page, so a shared Context must refuse the send."""
    _public_dns(monkeypatch)
    context = _RecordingContext()
    policy = NetworkPolicy(allowed_origins=("https://allowed.example",), allow_unsafe_methods=True)
    first_page = FakePage(url="https://allowed.example/a")
    second_page = FakePage(url="https://allowed.example/b")
    first = _session(first_page, context, policy)
    second = _session(second_page, context, policy)
    await first.install_guard()
    await second.install_guard()
    context.pages = [first_page, second_page]

    socket_route = _WebSocketRoute("wss://allowed.example/socket")
    handler = context.websocket_handler
    assert callable(handler)
    await handler(socket_route)
    forward = socket_route.message_handler
    assert callable(forward)

    # Page A holds the grant, but the socket may belong to Page B. Neither
    # message is forwarded because the Context is shared.
    with policy.authorize_unsafe_requests(first_page):
        forward("from page A")
        forward("from page B")
    assert socket_route.server.sent == []


async def test_a_single_page_context_still_forwards_during_an_authorized_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    context = _RecordingContext()
    policy = NetworkPolicy(allowed_origins=("https://allowed.example",), allow_unsafe_methods=True)
    page = FakePage(url="https://allowed.example/")
    session = _session(page, context, policy)
    await session.install_guard()
    context.pages = [page]

    socket_route = _WebSocketRoute("wss://allowed.example/socket")
    handler = context.websocket_handler
    assert callable(handler)
    await handler(socket_route)
    forward = socket_route.message_handler
    assert callable(forward)

    with policy.authorize_unsafe_requests(page):
        forward("authorized")
    assert socket_route.server.sent == ["authorized"]


async def test_an_external_task_cancel_records_a_operation_that_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.Task.cancel()` must block later runs, not only a deadline miss."""
    monkeypatch.setattr("jev_browser_py.runtime.DEADLINE_SETTLE_SECONDS", 0.05)
    started = asyncio.Event()
    release = asyncio.Event()

    class StubbornExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_one(self, *, proposal: object, snapshot: object, **kwargs: object) -> object:
            del proposal, snapshot, kwargs
            self.calls += 1
            started.set()
            while not release.is_set():
                try:
                    await asyncio.wait_for(release.wait(), timeout=0.01)
                except (TimeoutError, asyncio.CancelledError):
                    continue
            return SimpleNamespace(state=SimpleNamespace(value="acted"), detail="acted", error_code=None)

    page = FakePage()
    stubborn = StubbornExecutor()
    stubborn.page = page  # type: ignore[attr-defined]
    snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    snapshotter.page = page  # type: ignore[attr-defined]

    run_task = asyncio.create_task(
        run(
            "scroll",
            transport=_zero_cost_transport("scroll_down"),
            snapshotter=snapshotter,
            executor=stubborn,
            limits=RunLimits(max_steps=1, max_seconds=30),
        )
    )
    from jev_browser_py.session import _pending_page_operation

    await asyncio.wait_for(started.wait(), timeout=5)
    run_task.cancel()
    # Always let the stubborn child finish, even when an assertion fails: a
    # regression must fail the test, never hang the suite.
    try:
        outcomes = await asyncio.gather(run_task, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert stubborn.calls == 1

        # The fix under test: an external cancel records the child that refused
        # to stop, so the Page is visibly still occupied at this moment.
        assert _pending_page_operation(page) is not None

        second_snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
        second_snapshotter.page = page  # type: ignore[attr-defined]
        second_executor = FakeExecutor()
        second_executor.page = page  # type: ignore[attr-defined]
        second = await run(
            "scroll",
            transport=_zero_cost_transport("scroll_down"),
            snapshotter=second_snapshotter,
            executor=second_executor,
            limits=RunLimits(max_steps=1, max_seconds=0.05),
        )

        assert second.status is RunStatus.BLOCKED
        assert second.error_code is ErrorCode.UNKNOWN_EFFECT
        assert second_snapshotter.observed == []
        assert second_executor.action_count == 0
    finally:
        release.set()
        await asyncio.sleep(0.05)

    assert _pending_page_operation(page) is None

    third_snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    third_snapshotter.page = page  # type: ignore[attr-defined]
    third_executor = FakeExecutor()
    third_executor.page = page  # type: ignore[attr-defined]
    third = await run(
        "scroll",
        transport=_zero_cost_transport("scroll_down"),
        snapshotter=third_snapshotter,
        executor=third_executor,
        limits=RunLimits(max_steps=1, max_seconds=5),
    )

    assert third.status is not RunStatus.BLOCKED
    assert third_executor.action_count == 1


async def test_an_external_cancel_that_the_operation_honours_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jev_browser_py.runtime.DEADLINE_SETTLE_SECONDS", 0.05)
    started = asyncio.Event()

    class CooperativeExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_one(self, *, proposal: object, snapshot: object, **kwargs: object) -> object:
            del proposal, snapshot, kwargs
            self.calls += 1
            started.set()
            await asyncio.sleep(30)
            raise AssertionError("cancelled operation must not return")

    page = FakePage()
    executor = CooperativeExecutor()
    executor.page = page  # type: ignore[attr-defined]
    snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    snapshotter.page = page  # type: ignore[attr-defined]

    run_task = asyncio.create_task(
        run(
            "scroll",
            transport=_zero_cost_transport("scroll_down"),
            snapshotter=snapshotter,
            executor=executor,
            limits=RunLimits(max_steps=1, max_seconds=30),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    run_task.cancel()
    outcomes = await asyncio.gather(run_task, return_exceptions=True)
    assert isinstance(outcomes[0], asyncio.CancelledError)

    from jev_browser_py.session import _pending_page_operation

    # A child that stopped promptly leaves no lasting block on the Page.
    assert _pending_page_operation(page) is None

    second_snapshotter = FakeSnapshotter([make_snapshot(action_space=sample_action_space())])
    second_snapshotter.page = page  # type: ignore[attr-defined]
    second_executor = FakeExecutor()
    second_executor.page = page  # type: ignore[attr-defined]
    second = await run(
        "scroll",
        transport=_zero_cost_transport("scroll_down"),
        snapshotter=second_snapshotter,
        executor=second_executor,
        limits=RunLimits(max_steps=1, max_seconds=5),
    )

    assert second.status is not RunStatus.BLOCKED
    assert second_executor.action_count == 1
