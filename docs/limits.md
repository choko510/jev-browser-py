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
- **Loopback is not implicit.** An exact loopback origin in `allowed_origins` or
  the broader explicit `allow_loopback=True` is required. `allow_private=True`
  does not grant loopback access.
- A real Playwright `Page` passed to `run()` must have a `BrowserSession` network guard
  registered by `open_session`; otherwise the run is blocked. Direct low-level executor calls
  remain caller-owned and do not receive the runtime's automatic action-scoped write grant.
- The **network policy** routes observed HTTP resources and WebSocket endpoints
  through the Context allow-list; redirect responses are blocked. On an injected
  page, its WebSocket route remains until the caller closes that Context. Unsafe
  methods (all methods other than GET/HEAD/OPTIONS) are denied unless
  `allow_unsafe_methods=True` and an authorized external-write/ambiguous action
  is active for that same Page. The CLI `--allow-external-write` enables both
  the ActionPolicy and network gates. The guard is *not* an egress firewall:
  preconnects, existing service workers, DNS rebinding, and platform-level
  traffic may bypass the Python layer. Use a dedicated Context and real
  isolation for untrusted goals.
- **Model URL redaction** removes URL userinfo, fragments, every query value
  (not only recognized secret keys), and long opaque path segments. This is a
  conservative token heuristic, not a complete PII detector; configured-secret
  redaction remains an additional pass.
- **Time budget:** `max_seconds` is one monotonic run deadline applied to page
  observation, providers/generators, policy, executor, and verifier awaits. A
  timeout stops the run and prevents success. It cannot recall a request already
  accepted by a remote provider; the OpenAI-compatible typing request receives
  the remaining timeout, while a cancelled worker may take time to terminate.
- **Cost budget:** each Jev transport and text generator must implement
  `CostEstimator.estimate_max_cost_usd`; missing/invalid/over-budget estimates
  stop before dispatch. TypeSafe reserves a UTF-8 byte bound plus 65,536 bytes
  of SDK/schema overhead at its published input rate. OpenAI-compatible typing
  requires a declared per-call upper bound (CLI:
  `--typing-max-cost-per-call`). `RunResult.estimated_cost_usd` reports the
  current reservation. This is not a provider-enforced invoice cap: hidden
  provider billing overhead or already-accepted work can differ, so configure
  spending limits at the provider as well.
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
