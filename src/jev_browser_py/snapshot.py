"""Playwright page observation: bounded, stamped, and redacted.

The snapshotter is the only code that reads the page. It stamps ephemeral
`data-jev-id` attributes so the executor can resolve exactly the element the
model chose, then returns a :class:`PageSnapshot` whose every model-facing
string has passed through the redactor.

Design notes:

* Extraction runs as one in-page task, capped before stamping, so a page with
  thousands of hidden boilerplate nodes cannot crowd out real controls.
* `document.querySelectorAll("[data-jev-id]")` is cleared first; a stale stamp
  would make one selector match two elements.
* The excerpt prefers *visible* text (modal, else in-viewport text) because the
  start of `body.innerText` is usually navigation, never the dialog a task
  cares about.
* Nothing here navigates, clicks, or waits indefinitely; every wait is bounded.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .action_space import MAX_RAW_ELEMENTS, RawElement, build_action_space, raw_from_mapping
from .contracts import PageSnapshot, RedactionStatus, SnapshotTooLargeError
from .redaction import RedactionFailure, Redactor, build_redactor

__all__ = [
    "EXCERPT_CHARS",
    "SELECTOR",
    "TEXT_EXCERPT_CHARS",
    "SnapshotOptions",
    "Snapshotter",
    "snapshot_page",
]

EXCERPT_CHARS = 1_500
TEXT_EXCERPT_CHARS = 4_000
MAX_VISIBLE_NODES = 5_000
MAX_HTML_BYTES = 2_000_000

SELECTOR = (
    "a[href], button, details > summary, input, textarea, select, "
    '[role="button"], [role="link"], [role="searchbox"], [role="textbox"], '
    '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], '
    '[role="option"], [role="tab"], [role="switch"], [role="radio"], '
    '[role="checkbox"]'
)

_EXTRACT_SCRIPT = """
({ selector, cap, labelCap, includePassword }) => {
  document.querySelectorAll("[data-jev-id]").forEach((el) => el.removeAttribute("data-jev-id"));
  const clean = (s) => (s ?? "").replace(/\\s+/g, " ").trim();
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (out.length >= cap) break;
    const tag = el.tagName.toLowerCase();
    const typeAttr = (el.getAttribute("type") || "").toLowerCase();
    const roleAttr = el.getAttribute("role") || "";
    const style = getComputedStyle(el);
    if (!el.getClientRects().length || style.display === "none" || style.visibility === "hidden") continue;
    let hiddenDisclosure = false;
    for (let ancestor = el.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor instanceof HTMLDetailsElement && !ancestor.open &&
          !ancestor.querySelector(":scope > summary")?.contains(el)) hiddenDisclosure = true;
    }
    if (hiddenDisclosure) continue;
    const disabled = el.disabled === true || el.getAttribute("aria-disabled") === "true";
    const state = [];
    if (tag === "input" && (typeAttr === "checkbox" || typeAttr === "radio")) {
      state.push(el.indeterminate ? "mixed" : el.checked ? "checked" : "unchecked");
    }
    for (const [attr, yes, no] of [
      ["aria-checked", "checked", "unchecked"],
      ["aria-pressed", "pressed", "not pressed"],
      ["aria-expanded", "expanded", "collapsed"],
      ["aria-selected", "selected", "not selected"],
    ]) {
      if (tag === "input" && attr === "aria-checked") continue;
      const value = el.getAttribute(attr);
      if (value === "true") state.push(yes);
      if (value === "false") state.push(no);
      if (value === "mixed" && attr !== "aria-expanded") state.push("mixed");
    }
    if (tag === "summary") state.push(el.parentElement?.open ? "expanded" : "collapsed");
    const sort = el.closest('[aria-sort]')?.getAttribute("aria-sort");
    if (sort === "ascending" || sort === "descending") state.push("sorted " + sort);
    const labelledby = clean(
      (el.getAttribute("aria-labelledby") || "")
        .split(/\\s+/)
        .map((ref) => document.getElementById(ref)?.textContent ?? "")
        .join(" ")
    );
    const nativeLabels = clean(Array.from(el.labels ?? []).map((l) => l.textContent ?? "").join(" "));
    const valueAttr = el.getAttribute("value");
    const valueLabel = tag === "input" && typeAttr === "submit"
      ? (valueAttr ?? "Submit")
      : tag === "input" && typeAttr === "button" ? (valueAttr ?? "") : "";
    const label = clean(
      labelledby ||
        clean(el.getAttribute("aria-label")) ||
        nativeLabels ||
        clean(valueLabel) ||
        clean(el.getAttribute("placeholder")) ||
        clean(el.getAttribute("title")) ||
        clean(el.innerText) ||
        clean(el.textContent) ||
        ""
    );
    const href = tag === "a" ? (el.getAttribute("href") || "") : "";
    const clickable =
      ["a", "button", "summary"].includes(tag) ||
      ["button", "link", "menuitem", "menuitemradio", "menuitemcheckbox", "option",
       "tab", "switch", "radio", "checkbox"].includes(roleAttr) ||
      ["submit", "button", "checkbox", "radio"].includes(typeAttr);
    const selectable = tag === "select";
    const passwordInput = tag === "input" && typeAttr === "password";
    if (passwordInput && !includePassword) continue;
    const typeable =
      !passwordInput &&
      (tag === "textarea" ||
        (tag === "input" &&
          !["submit", "button", "checkbox", "radio", "file", "hidden", "range", "password"].includes(typeAttr)) ||
        ["searchbox", "textbox"].includes(roleAttr));
    const searchField = (tag === "input" && typeAttr === "search") || roleAttr === "searchbox";
    const submitControl =
      (tag === "button" && (typeAttr === "submit" || (!el.hasAttribute("type") && el.closest("form") !== null))) ||
      (tag === "input" && typeAttr === "submit");
    const enterSubmittable = typeable && tag !== "textarea";
    const formMethod = (el.getAttribute("formmethod") || el.form?.method || "").toLowerCase();
    if (!clickable && !typeable && !selectable && !passwordInput) continue;
    const attr = `j${out.length + 1}`;
    el.setAttribute("data-jev-id", attr);
    const options = tag === "select"
      ? Array.from(el.options)
          .map((o, i) => ({ i, label: clean(o.label || o.value || "").slice(0, 200) }))
          .filter((o) => o.label.length > 0)
          .slice(0, 200)
      : undefined;
    out.push({
      attr, tag, role: roleAttr || tag, text: label.slice(0, labelCap), href,
      typeAttr, formMethod, clickable, typeable, selectable, searchField, submitControl,
      enterSubmittable, passwordInput: passwordInput || undefined, options, state, disabled,
    });
  }
  return out;
}
"""

_OBSERVABLES_SCRIPT = """
({ excerptCap }) => {
  const clean = (s) => (s ?? "").replace(/\\s+/g, " ").trim();
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const words = (document.body?.innerText ?? "").replace(/\\s+/g, " ");
  const modal = [...document.querySelectorAll('[aria-modal="true"], dialog:modal')].filter(shown).at(-1);
  let visible = "";
  if (modal) {
    visible = clean(modal.innerText);
  } else {
    const parts = [];
    let length = 0;
    let visited = 0;
    const walker = document.createTreeWalker(document.body ?? document.documentElement, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node && length < excerptCap && visited < 5000; node = walker.nextNode()) {
      visited++;
      const text = clean(node.textContent);
      if (!text || !node.parentElement || !shown(node.parentElement)) continue;
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || r.bottom <= 0 || r.top >= innerHeight ||
          r.right <= 0 || r.left >= innerWidth) continue;
      parts.push(text);
      length += text.length + 1;
    }
    visible = parts.join(" ");
  }
  const fingerprint =
    `${document.body?.innerText?.length ?? 0}:` +
    `${document.querySelectorAll("a,button,input,select,textarea").length}:${location.href}`;
  return {
    url: location.href,
    title: document.title,
    textLength: document.body?.innerText?.length ?? 0,
    excerpt: words.slice(0, excerptCap),
    visibleExcerpt: visible.slice(0, excerptCap),
    fingerprint,
  };
}
"""


@dataclass(frozen=True, slots=True)
class SnapshotOptions:
    """Limits applied to one observation; all are enforced."""

    excerpt_chars: int = EXCERPT_CHARS
    element_cap: int = 240
    max_page_bytes: int = MAX_HTML_BYTES
    max_raw_elements: int = MAX_RAW_ELEMENTS
    password_active: bool = False
    label_cap: int = 80


class Snapshotter:
    """Turns a Playwright `Page` into a bounded, redacted `PageSnapshot`."""

    def __init__(
        self,
        page: Any,
        *,
        redactor: Redactor | None = None,
        options: SnapshotOptions | None = None,
        secrets: list[str] | None = None,
    ) -> None:
        self._page = page
        self._options = options or SnapshotOptions()
        self._configured_secrets: list[str] = list(secrets or [])
        if redactor is not None:
            self._redactor = redactor
        else:
            self._redactor = build_redactor(self._configured_secrets)
        self._generation = 0

    @property
    def redactor(self) -> Redactor:
        return self._redactor

    @property
    def page(self) -> Any:
        """The Playwright Page observed by this snapshotter."""
        return self._page

    def ensure_secrets(self, secrets: list[str]) -> None:
        """Guarantee these values are redacted before any observation.

        Called by the run loop when the caller passes secrets separately. A
        secret that is only *stored* would be a silent leak, so this rebuilds
        the redactor and immediately verifies that each new value is actually
        removed from a probe string.
        """
        if not secrets:
            return
        combined = list(dict.fromkeys([*self._configured_secrets, *secrets]))
        self._configured_secrets = combined
        self._redactor = build_redactor(combined)
        for secret in secrets:
            if secret in self._redactor.redact(f"probe {secret} probe"):
                raise RedactionFailure("a configured secret was not registered for redaction")

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        """Advance the DOM generation; called after every executed action."""
        self._generation += 1
        return self._generation

    async def observe(self, *, bump: bool = False) -> PageSnapshot:
        """Capture one snapshot.

        `bump=True` invalidates every proposal bound to an earlier generation
        before reading, which is exactly what must happen after an action.
        """
        if bump:
            self.bump_generation()
        options = self._options
        page = self._page
        html_bytes = await self._html_bytes()
        if html_bytes > options.max_page_bytes:
            raise SnapshotTooLargeError(
                f"page HTML is {html_bytes} bytes, over the {options.max_page_bytes} byte limit"
            )
        try:
            observables = await page.evaluate(
                _OBSERVABLES_SCRIPT, {"excerptCap": max(options.excerpt_chars, TEXT_EXCERPT_CHARS)}
            )
        except Exception as error:
            raise SnapshotTooLargeError("the page could not be observed; it may be navigating or closed") from error
        try:
            raw_payload = await page.evaluate(
                _EXTRACT_SCRIPT,
                {
                    "selector": SELECTOR,
                    "cap": min(options.max_raw_elements, MAX_RAW_ELEMENTS),
                    "labelCap": options.label_cap,
                    "includePassword": options.password_active,
                },
            )
        except Exception as error:
            raise SnapshotTooLargeError(
                "the page could not be read for interactive elements; it may be navigating or closed"
            ) from error
        raw: list[RawElement] = []
        for item in raw_payload or ():
            if not isinstance(item, dict):
                continue
            element = raw_from_mapping(item)
            raw.append(self._redact_element(element))
        space = build_action_space(raw, password_active=options.password_active, cap=options.element_cap)
        url = await self._url()
        title = str(observables.get("title") or "")
        excerpt_source = str(observables.get("visibleExcerpt") or observables.get("excerpt") or "")
        excerpt = self._redact(excerpt_source)[: options.excerpt_chars]
        redacted_url = self._redact(url)
        redacted_title = self._redact(title)[:200]
        status = RedactionStatus.REDACTED if self._redactor.has_secrets() else RedactionStatus.NO_SECRETS
        return PageSnapshot(
            snapshot_id=uuid.uuid4().hex,
            generation=self._generation,
            url=redacted_url,
            title=redacted_title,
            observed_at=time.time(),
            excerpt=excerpt,
            frame_ref=None,
            action_space=space,
            truncated=space.truncated,
            redaction=status,
            html_bytes=html_bytes,
            page_fingerprint=str(observables.get("fingerprint") or "")[:200],
        )

    async def _url(self) -> str:
        try:
            url = self._page.url
            return url() if callable(url) else str(url)
        except Exception:  # noqa: BLE001 - a closed page has no URL
            return ""

    async def _html_bytes(self) -> int:
        try:
            content = await self._page.content()
        except Exception:  # noqa: BLE001 - treat an unreadable page as empty
            return 0
        return len(str(content).encode("utf-8", errors="replace"))

    def _redact_element(self, element: RawElement) -> RawElement:
        if not self._redactor.has_secrets():
            return element
        return RawElement(
            attr=element.attr,
            tag=element.tag,
            role=element.role,
            text=self._redact(element.text)[: self._options.label_cap],
            href=self._redact(element.href),
            type_attr=element.type_attr,
            form_method=element.form_method,
            clickable=element.clickable,
            typeable=element.typeable,
            selectable=element.selectable,
            password_input=element.password_input,
            search_field=element.search_field,
            submit_control=element.submit_control,
            enter_submittable=element.enter_submittable,
            options=tuple((index, self._redact(label)[:200]) for index, label in element.options),
            state=element.state,
            enabled=element.enabled,
        )

    def _redact(self, text: str) -> str:
        try:
            return self._redactor.redact(text)
        except RedactionFailure as error:
            raise SnapshotTooLargeError("redaction failed; refusing to expose page text to the model") from error


async def snapshot_page(
    page: Any,
    *,
    redactor: Redactor | None = None,
    secrets: list[str] | None = None,
    options: SnapshotOptions | None = None,
) -> PageSnapshot:
    """Convenience wrapper for a single observation."""
    return await Snapshotter(page, redactor=redactor, options=options, secrets=secrets).observe()


def stable_text_digest(text: str) -> str:
    """Digest used by tests and the verifier; never includes secrets."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()
