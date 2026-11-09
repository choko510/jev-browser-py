"""Browser ownership, injection, and the network guard.

Ownership rules, enforced structurally:

* A :class:`BrowserSession` created with `start_url` owns its browser and
  closes it on exit.
* A session created with `page=` **never** closes the caller's page, context,
  or browser. Closing an injected page is a caller decision.
* Only one tab action may run at a time; :meth:`BrowserSession.action_lock`
  serializes them.

The network guard is a best-effort standalone control. It aborts top-level
navigations, popups, and subresource requests outside an allow-list, and it
refuses to resolve a hostname to a private/loopback/link-local address at the
*policy* level. It is explicitly not a substitute for egress isolation: see
`NetworkPolicy` docstring.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import threading
import weakref
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .contracts import (
    NetworkGuardError,
    PageSnapshot,
    RunResult,
)

__all__ = [
    "LOCAL_HOSTS",
    "BrowserOwner",
    "BrowserSession",
    "NetworkPolicy",
    "is_private_host",
    "open_session",
]

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss", "about", "data", "blob"})


@dataclass
class NetworkPolicy:
    """Allow-list based egress policy for one session.

    **This is not an egress firewall.** The Context route checks observed
    HTTP/WebSocket destinations, but browser preconnects and service-worker or
    platform traffic may bypass it. HTTP redirect responses are blocked rather
    than followed. Use real isolation (container, proxy, or host firewall)
    before exposing the library to untrusted tasks on the public internet.

    Defaults are intentionally restrictive: loopback fixtures are allowed,
    arbitrary internet hosts are allowed only when explicitly listed.
    """

    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allow_loopback: bool = True
    allow_private: bool = False
    allowed_schemes: frozenset[str] = _DEFAULT_ALLOWED_SCHEMES
    block_downloads: bool = True
    block_new_tabs: bool = False

    def __post_init__(self) -> None:
        normalized = []
        for origin in self.allowed_origins:
            parts = urlsplit(origin)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError(f"allowed origin must be http(s)://host, got {origin!r}")
            normalized.append(f"{parts.scheme}://{parts.netloc}".lower())
        object.__setattr__(self, "allowed_origins", tuple(dict.fromkeys(normalized)))
        object.__setattr__(
            self,
            "allowed_hosts",
            tuple(host.lower() for host in dict.fromkeys(self.allowed_hosts) if host),
        )

    def origin_allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            return False
        if scheme in {"about", "data", "blob"}:
            return True
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if self.allow_loopback and host in LOCAL_HOSTS:
            return True
        if is_private_host(host):
            return self.allow_private
        origin_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        origin = f"{origin_scheme}://{parts.netloc}".lower()
        return origin in self.allowed_origins or host in self.allowed_hosts

    def check(self, url: str) -> None:
        if not self.origin_allowed(url):
            raise NetworkGuardError(
                f"navigation to {urlsplit(url).scheme}://{urlsplit(url).netloc or '(unknown)'} "
                "is blocked by the network policy"
            )


def is_private_host(host: str) -> bool:
    """True for loopback, private, link-local, or unresolvable hosts."""
    if not host:
        return True
    if host.lower() in LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None)
        except OSError:
            return True
        for info in resolved:
            try:
                candidate = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError):
                return True
            if (
                candidate.is_private
                or candidate.is_loopback
                or candidate.is_link_local
                or candidate.is_reserved
                or candidate.is_multicast
            ):
                return True
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


@dataclass
class BrowserOwner:
    """Owns a Playwright browser/context lifecycle for one session."""

    browser: Any
    context: Any
    page: Any
    owns_browser: bool
    owns_context: bool
    owns_page: bool
    playwright: Any = None
    _closed: bool = field(default=False, repr=False)

    async def close(self) -> None:
        """Close only what this owner created, exactly once.

        Cleanup is best-effort: a page that is already gone must not mask the
        run result, and a failure to close one layer must not skip the next.
        """
        if self._closed:
            return
        self._closed = True
        if self.owns_page:
            with contextlib.suppress(Exception):
                await self.page.close()
        if self.owns_context:
            with contextlib.suppress(Exception):
                await self.context.close()
        if self.owns_browser:
            with contextlib.suppress(Exception):
                await self.browser.close()
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> BrowserOwner:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def _safe_abort(route: Any) -> None:
    try:
        await route.abort()
    except Exception:  # noqa: BLE001 - the request already completed
        return


_PAGE_LOCKS: dict[int, tuple[weakref.ReferenceType[Any], asyncio.Lock]] = {}
_PAGE_LOCKS_GUARD = threading.RLock()


def _forget_page_lock(key: int, reference: weakref.ReferenceType[Any]) -> None:
    with _PAGE_LOCKS_GUARD:
        current = _PAGE_LOCKS.get(key)
        if current is not None and current[0] is reference:
            del _PAGE_LOCKS[key]


def _page_action_lock(page: Any) -> asyncio.Lock:
    """Return one shared action lock for every wrapper around a Page object."""
    key = id(page)
    with _PAGE_LOCKS_GUARD:
        current = _PAGE_LOCKS.get(key)
        if current is not None and current[0]() is page:
            return current[1]
        try:
            reference = weakref.ref(page, lambda ref: _forget_page_lock(key, ref))
        except TypeError as error:
            raise TypeError("page objects must support weak references for safe action serialization") from error
        lock = asyncio.Lock()
        _PAGE_LOCKS[key] = (reference, lock)
        return lock


@dataclass
class BrowserSession:
    """One owned page plus its policy, lock, and snapshot bound method."""

    page: Any
    owner: BrowserOwner
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    _lock: asyncio.Lock = field(init=False, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = _page_action_lock(self.page)

    @property
    def action_lock(self) -> asyncio.Lock:
        """Shared Page lock; `run()` acquires it automatically for the whole run."""
        return self._lock

    async def navigate(self, url: str, *, timeout_ms: int = 15_000) -> None:
        """Navigate under the network policy, never bypassing it."""
        self.network.check(url)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except NetworkGuardError:
            raise
        except Exception as error:
            raise NetworkGuardError("navigation failed before the page could be trusted") from error

    async def install_guard(self) -> None:
        """Install awaited context-wide HTTP and WebSocket policy routes.

        The guard is installed before any owned-page navigation and applies to
        every HTTP resource type and popup in the Context. Redirect responses
        are blocked because Playwright does not re-run the route handler for
        every server redirect hop. Playwright also has no API to remove
        WebSocket routes; on an injected Context this policy remains attached
        until the caller closes that Context. This is not an egress firewall.
        """
        context = self.owner.context
        if context is None or self.page is None:
            raise NetworkGuardError("a browser context is required to install the network guard")
        websocket_route = getattr(context, "route_web_socket", None)
        if websocket_route is None:
            raise NetworkGuardError("this Playwright Context cannot enforce WebSocket policy")
        policy = self.network

        async def _route_handler(route: Any) -> None:
            try:
                request_url = str(getattr(route.request, "url", "") or "")
            except Exception:  # noqa: BLE001 - a torn-down request
                await _safe_abort(route)
                return
            try:
                allowed = policy.origin_allowed(request_url)
            except Exception:  # noqa: BLE001 - malformed or unresolvable URLs fail closed
                allowed = False
            if not allowed or (policy.block_downloads and "/download" in request_url.lower()):
                await _safe_abort(route)
                return
            try:
                # Playwright does not route every server-side redirect hop back
                # through the handler. Fetch one hop only and fail closed on a
                # redirect instead of allowing an unchecked second request.
                response = await route.fetch(max_redirects=0)
            except Exception:  # noqa: BLE001 - a failed fetch is never retried
                await _safe_abort(route)
                return
            if response.status in {300, 301, 302, 303, 307, 308} and response.headers.get("location"):
                await _safe_abort(route)
                return
            if policy.block_downloads and "attachment" in response.headers.get("content-disposition", "").lower():
                await _safe_abort(route)
                return
            try:
                await route.fulfill(response=response)
            except Exception:  # noqa: BLE001 - the request already completed
                return

        async def _websocket_handler(route: Any) -> None:
            try:
                websocket_url = str(getattr(route, "url", "") or "")
                allowed = policy.origin_allowed(websocket_url)
            except Exception:  # noqa: BLE001 - malformed or unresolvable URLs fail closed
                allowed = False
            if not allowed:
                await route.close(code=1008, reason="blocked by the session network policy")
                return
            await route.connect()

        try:
            await websocket_route("**/*", _websocket_handler)
            await context.route("**/*", _route_handler)
        except Exception as error:  # noqa: BLE001 - failure to install is fail-closed
            raise NetworkGuardError("the browser network guard could not be installed") from error

    async def close(self) -> None:
        """Close only owned browser resources after this Page leaves the run lock.

        Routes attached to an injected Context remain until that caller-owned
        Context closes; Playwright cannot remove its WebSocket route.
        """
        if self._closed:
            return
        async with self.action_lock:
            if self._closed:
                return
            self._closed = True
            await self.owner.close()

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> BrowserSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def open_session(
    *,
    start_url: str | None = None,
    page: Any | None = None,
    network: NetworkPolicy | None = None,
    headless: bool = True,
    browser_type: str = "chromium",
    install_guard: bool = True,
    context_options: dict[str, Any] | None = None,
) -> BrowserSession:
    """Open an owned session, or adopt an injected page without owning it.

    Adopting a page never launches a browser and never closes the page: the
    caller keeps full lifecycle control, which is what a Harness Browser
    Worker needs.
    """
    if page is not None and start_url is not None:
        raise ValueError("pass either start_url or page, not both")
    if page is None and start_url is None:
        raise ValueError("one of start_url or page is required")
    policy = network or NetworkPolicy()
    if page is not None:
        owner = BrowserOwner(
            browser=None,
            context=getattr(page, "context", None),
            page=page,
            owns_browser=False,
            owns_context=False,
            owns_page=False,
        )
        session = BrowserSession(page=page, owner=owner, network=policy)
        try:
            if install_guard:
                await session.install_guard()
        except Exception:
            await session.close()
            raise
        return session
    assert start_url is not None
    policy.check(start_url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - declared dependency
        raise NetworkGuardError("playwright is not installed") from error
    playwright = await async_playwright().start()
    browser: Any | None = None
    context: Any | None = None
    owned_page: Any | None = None
    owned_session: BrowserSession | None = None
    try:
        launcher = getattr(playwright, browser_type)
        browser = await launcher.launch(headless=headless)
        browser_context_options = dict(context_options or {})
        if install_guard:
            if browser_context_options.get("service_workers", "block") != "block":
                raise NetworkGuardError("service_workers must be blocked when the network guard is enabled")
            browser_context_options["service_workers"] = "block"
        context = await browser.new_context(**browser_context_options)
        owned_page = await context.new_page()
        owner = BrowserOwner(
            browser=browser,
            context=context,
            page=owned_page,
            owns_browser=True,
            owns_context=True,
            owns_page=True,
            playwright=playwright,
        )
        owned_session = BrowserSession(page=owned_page, owner=owner, network=policy)
        if install_guard:
            await owned_session.install_guard()
        await owned_session.navigate(start_url)
        return owned_session
    except Exception:
        if owned_session is not None:
            await owned_session.close()
        else:
            if owned_page is not None:
                with contextlib.suppress(Exception):
                    await owned_page.close()
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()
            with contextlib.suppress(Exception):
                await playwright.stop()
        raise


def assert_allowed_navigation(policy: NetworkPolicy, urls: Iterable[str]) -> None:
    """Raise on the first disallowed URL; used by host adapters and the CLI."""
    for url in urls:
        policy.check(url)


def same_origin(a: str, b: str) -> bool:
    first, second = urlsplit(a), urlsplit(b)
    return (first.scheme, first.netloc) == (second.scheme, second.netloc)


def summarize_session(session: BrowserSession) -> dict[str, Any]:
    """Secret-free session description for traces."""
    return {
        "owned": session.owner.owns_page,
        "url": safe_url(getattr(session.page, "url", "")),
        "closed": session.closed,
    }


def safe_url(url: Any) -> str:
    from .redaction import redact_query_string

    text = url() if callable(url) else str(url or "")
    return redact_query_string(text)


def private_hosts_in(urls: Sequence[str]) -> tuple[str, ...]:
    """Diagnostic helper: which hosts would the policy classify as private."""
    return tuple(
        sorted({urlsplit(url).hostname or "" for url in urls if is_private_host(urlsplit(url).hostname or "")})
    )


async def snapshot_of(session: BrowserSession) -> PageSnapshot:
    """Convenience: observe the session's current page."""
    from .snapshot import Snapshotter

    return await Snapshotter(session.page).observe()


async def close_quietly(result: RunResult | None, session: BrowserSession) -> None:
    """Close a session at the end of a run, preserving the run result."""
    del result
    await session.close()
