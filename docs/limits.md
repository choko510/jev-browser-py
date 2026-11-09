# Supported-site boundary and known limits (draft; not a release approval)

This document is the honest scope statement the release gate requires. If a
site needs something below, the library stops with a classified status rather
than pretending to succeed.

## Implemented draft scope

| Capability | Behaviour |
|---|---|
| Static and server-rendered pages | Full observe → judge → act loop |
| Same-origin links and multi-page flows | Click, wait, re-observe, verify |
| Native `<select>` | Offered as `select_eN`; a second typed choice picks the option by live DOM index |
| Search boxes (`type=search`, `role=searchbox`) | `search_eN` fills only; a separate `submit_eN` proposes Enter. GET search forms are read-only; POST or unknown submission needs explicit policy permission |
| Text fields | `type_eN` (fill only); a separate `submit_eN` performs Enter, so a fill never submits implicitly |
| Checkboxes/radios/switches | Offered as `click_eN`, with state in the description |
| Scrolling and back | `scroll_down`, `scroll_up`, `back` control actions |
| Disabled `<details>` descendants | Excluded (only the open summary subtree is actionable) |
| Hidden inputs (checkbox/radio behind a label) | Resolved through the associated label |
| SPA re-render on the same document | Detected as a new snapshot; stale proposals are dropped |

## Not supported; these stop safely

| Scenario | Result |
|---|---|
| CAPTCHA / Cloudflare interstitial | `BLOCKED` or `NEEDS_HUMAN`; never bypassed |
| MFA / login wall | `NEEDS_HUMAN`; password fields are hidden unless a credential source is configured |
| Cross-origin navigation not in the allow-list | `BLOCKED` (`NETWORK_BLOCKED`) |
| HTTP redirect response | `BLOCKED`; start directly at an explicitly allowed URL |
| iframe targets | Out of scope; the frame's controls are not offered |
| Shadow DOM internals | Out of scope; the host element may still be clickable |
| Popups / new tabs | Not followed in this draft; a popup that replaces the page is treated as a navigation |
| Canvas-only UIs | No actionable elements are found; the run reports an empty action space |
| `<input type="file">` | Never offered |
| Downloads | Aborted by the default network policy |
| Paywalled or authenticated content | `NEEDS_HUMAN` |

## Security boundary

- The **policy** is deny-by-default: `read` and `local_input` are allowed,
  `ambiguous` and `external_write` require an explicit caller opt-in.
- The **network policy** routes observed HTTP resources and WebSocket endpoints
  through the Context allow-list; redirect responses are blocked. On an injected
  page, its WebSocket route remains until the caller closes that Context. It is
  *not* an egress firewall: preconnects, existing service workers, DNS rebinding,
  and platform-level traffic may bypass the Python layer. Use a dedicated Context
  and real isolation for untrusted goals.
- The **verifier** is independent of the model. It verifies only explicit goal phrases; a goal with no extractable evidence target remains `UNVERIFIED` unless the caller supplies a task-specific verifier. A `done` proposal or a `goal_done > 0.85` still requires verifier evidence.
- **Echoes are not evidence.** A results page that prints the agent's own
  query back is stripped before verification, so a search cannot verify
  itself.
- **No automatic retry** after an action whose effect may be unknown.

## Performance characteristics (measured locally)

Measured with `scripts/benchmark.py` on a Windows 11 host, Python 3.14.3,
Playwright Chromium headless, loopback fixture, scripted transport:

- snapshot observation: p50 ≈ 4.5 ms, p95 ≈ 37.2 ms
- complete 3-action verified task: ≈ 0.52 s

These numbers describe local DOM/browser overhead only. They say nothing about
model latency, public-site performance, or upstream parity. No speed or cost
advantage over the TypeScript implementation is claimed; that comparison is
recorded as `not measured`.
