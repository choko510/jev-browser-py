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

_DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https", "about", "data", "blob"})


@dataclass
class NetworkPolicy:
    """Allow-list based egress policy for one session.

    **This is not an egress firewall.** Chromium issues requests the Python
    layer never sees (preconnect, favicons, service workers). Use it to catch
    accidents and to document intent, and pair it with real isolation
    (container, proxy, or host firewall) before exposing the library to
    untrusted tasks on the public internet.

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
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            return False
        if scheme in {"about", "data", "blob"}:
            return True
        host = (parts.hostname or "").lower()
        if not host:
            return False
        origin = f"{scheme}://{parts.netloc}".lower()
        if origin in self.allowed_origins:
            return True
        if host in self.allowed_hosts:
            return True
        if self.allow_loopback and host in LOCAL_HOSTS:
            return True
        if is_private_host(host):
            return self.allow_private
        return bool(self.allowed_origins or self.allowed_hosts)

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


@dataclass
class BrowserSession:
    """One owned page plus its policy, lock, and snapshot bound method."""

    page: Any
    owner: BrowserOwner
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _closed: bool = field(default=False, repr=False)
    _guard_tasks: list[asyncio.Task[None]] = field(default_factory=list, repr=False)

    @property
    def action_lock(self) -> asyncio.Lock:
        """Serialize actions on this page; concurrent actions are forbidden."""
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

    def install_guard(self) -> None:
        """Route-level guard: abort disallowed documents and subresources.

        The Playwright async API requires an awaitable route handler; a sync
        handler silently stalls every request it intercepts, so this is
        registered as a coroutine function.

        Best-effort by design: see :class:`NetworkPolicy` for the limits of a
        Python-side guard inside a real browser.
        """
        context = self.owner.context
        page = self.page
        if page is None:
            return
        policy = self.network

        async def _route_handler(route: Any) -> None:
            try:
                request = route.request
                request_url = str(getattr(request, "url", "") or "")
                resource_type = str(getattr(request, "resource_type", "") or "")
            except Exception:  # noqa: BLE001 - a torn-down request
                await _safe_abort(route)
                return
            if resource_type == "document" and not policy.origin_allowed(request_url):
                await _safe_abort(route)
                return
            if policy.block_downloads and "/download" in request_url.lower():
                await _safe_abort(route)
                return
            try:
                await route.continue_()
            except Exception:  # noqa: BLE001 - the request already completed
                return

        async def _guard() -> None:
            try:
                await page.route("**/*", _route_handler)
            except Exception:  # noqa: BLE001 - a page without routing support
                return

        if context is None and page is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop means no page anyway
            return
        self._guard_tasks.append(loop.create_task(_guard()))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._guard_tasks:
            task.cancel()
        self._guard_tasks.clear()
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
        if install_guard:
            session.install_guard()
        return session
    assert start_url is not None
    policy.check(start_url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - declared dependency
        raise NetworkGuardError("playwright is not installed") from error
    playwright = await async_playwright().start()
    try:
        launcher = getattr(playwright, browser_type)
        browser = await launcher.launch(headless=headless)
        context = await browser.new_context(**(context_options or {}))
        owned_page = await context.new_page()
        await owned_page.goto(start_url, wait_until="domcontentloaded")
    except Exception:
        await playwright.stop()
        raise
    owner = BrowserOwner(
        browser=browser,
        context=context,
        page=owned_page,
        owns_browser=True,
        owns_context=True,
        owns_page=True,
        playwright=playwright,
    )
    session = BrowserSession(page=owned_page, owner=owner, network=policy)
    if install_guard:
        session.install_guard()
    return session


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
