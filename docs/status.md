# Status and evidence log

This file records what was actually built and executed, so the guide's
"design vs. implemented vs. verified" distinction stays honest. Update it when
a claim changes; never record an unexecuted check as passing.

## Implementation present (standalone draft; release gate not passed)

| Guide PR | Scope | Status |
|---|---|---|
| PR-01 | pyproject/uv.lock, contracts, questions, transport, judgment, mocks | Done |
| PR-02 | Playwright snapshot, action space, secret redaction | Done |
| PR-03 | Policy, single-action executor, pre-execution freshness | Done |
| PR-04 | Bounded loop, stop gates, verifier, typing | Done |
| PR-05 | Fault recovery, cancel/race, security hardening | Done |
| PR-06 | Scrapling `Selector` extraction with evidence | Done |
| PR-07 | Session ownership, SPA/iframe/popup scope | Done |
| PR-08 | Public API, CLI, packaging, Docker example | Done |
| PR-09 | Upstream comparison harness, benchmark, audit, docs | Partial; upstream comparison and paid smoke remain unmeasured |

## Executed checks

Environment: Windows 11, Python 3.14.3, Playwright 1.64.1 (bundled Chromium),
Scrapling 0.5.1, TypeSafe SDK 0.7.5.

```
python -m pytest -q                                      -> 270 passed
python -m pytest -q -m "not browser"                     -> 252 passed, 18 deselected
JEV_REQUIRE_BROWSER_TESTS=1 python -m pytest -q -m browser -> 18 passed, 252 deselected; no skips
python -m ruff check --no-cache .                       -> All checks passed
python -m ruff format --check .                          -> 40 files already formatted
python -m mypy src/jev_browser_py                       -> Success: no issues found (17 files)
python -m build                                          -> sdist + wheel built from this worktree
python examples/local_task.py                           -> status success, verified True (3 actions)
```

**Historical CI correction:** GitHub Actions run `42648478807` reported green on
prior HEAD `04939bc`, but its browser job logged 12 skipped and 219 deselected.
That run is not evidence that browser tests executed. The workflow now uses the
same absolute browser path for installation and testing, disables skip marks in
required mode, and fails if no browser test passes. Code/test run `42826391344` on `9afe9a7` passed every job after extracting provider-budget regressions to `test_provider_budgets.py`. Documentation follow-up run `42827933717` on `9de03bb` also passed quality, both required-browser jobs, package build, and wheel import; each browser log reported 15 passed and 238 deselected.

Browser tests (real Chromium, loopback fixtures, scripted judgment): 18 passed
locally with required mode enabled. They include the verified search path, a
same-origin JavaScript POST blocked for a read link, an explicitly authorized
POST form, redirects/subresources, local page-scope behavior, a shared Context
rejecting a second network policy, the same policy accepted for two Pages, and
two links whose query values mask identically staying two distinct actions; no
public site is used.

**Follow-up audit remediation (same Draft PR):**

| Finding | Owner | Regression |
|---|---|---|
| Shared Context could apply a later route's policy to all Pages | `session.py` `_register_context_network_policy` | `test_audit_followups.py`, `test_e2e_audit_followups.py` |
| WebSocket client messages were forwarded unconditionally | `session.py` `_websocket_handler` | `test_audit_followups.py` (default deny, authorized send, disabled policy) |
| A Page could borrow another Page's WebSocket approval in a shared Context | `session.py` `_context_is_shared` | `test_audit_followups.py` (shared Context refuses send; single-Page Context still forwards) |
| External `asyncio.Task.cancel()` did not record an operation that kept running | `runtime.py` `_settle_cancelled_child` | `test_audit_followups.py` (stubborn child blocks the next run; cooperative child leaves no block) |
| Masked URLs could collapse distinct links | `action_space.py` `raw_href_identity`, `snapshot.py` | `test_audit_followups.py`, `test_e2e_audit_followups.py` |
| Page-lock wait was outside `max_seconds`; a timed-out operation could overlap a later Run | `runtime.py` `_await_before_deadline`, `run()` | `test_audit_followups.py` (lock-wait timeout, abandoned-operation block) |

## Not executed here

- **Paid TypeSafe live smoke.** No `TYPESAFE_API_KEY` exists in this
  environment. `.github/workflows/live-smoke.yml` is manual-only and requires
  the secret; it has not been run from this workspace.
- **Upstream TypeScript comparison.** The TypeScript Jev Browser is not
  installed and a live model call needs a key. `scripts/benchmark.py` records
  `"upstream_comparison": {"status": "not measured"}` rather than inventing a
  ratio. This is the one PR-09 deliverable that remains unmeasured, and it is
  reported as such instead of being claimed.

## Guide test-ID mapping

| ID | Covered by |
|---|---|
| PYJEV-T01 | `test_judgment.py` (three typed judgments, usage) |
| PYJEV-T02 | `test_judgment.py` (ID/NaN/range/usage/model/unknown answer) |
| PYJEV-T03 | `test_action_space.py` (noise, dedupe, hidden, password, cap) |
| PYJEV-T04 | `test_e2e_local.py` (injected page never closed) |
| PYJEV-T05 | `test_runtime.py` (double match, stale fingerprint, stale snapshot) |
| PYJEV-T06 | `test_runtime.py`, `test_verifier.py` (done alone never succeeds) |
| PYJEV-T07 | `test_runtime.py` (full-confidence submit denied, zero actions) |
| PYJEV-T08 | `test_runtime.py`, `test_provider_budgets.py` (global awaited-stage deadlines, cancel, stuck) |
| PYJEV-T09 | `test_runtime.py`, `test_security.py` (terminal error, no retry) |
| PYJEV-T10 | `test_redaction.py`, `test_redaction_wiring.py`, `test_security.py` (unknown URL tokens at model boundaries) |
| PYJEV-T11 | `test_extraction.py` (socket-failing extraction, provenance) |
| PYJEV-T12 | `test_security.py`, `test_scope.py` (private IP, loopback, scheme, cross-origin, unsafe methods) |
| PYJEV-T13 | `test_runtime.py` (`UNKNOWN_EFFECT`, no automatic resend) |
| PYJEV-T14 | `test_security.py` (separate sessions, locks, pages) |
| PYJEV-T15 | `test_scope.py` (SPA, iframe, canvas, popup) |
| PYJEV-T16 | `test_e2e_local.py` (search → detail, read-link POST blocked, explicitly authorized submit verified) |
| PYJEV-T17 | `scripts/benchmark.py` (p50/p95, tokens, elapsed, RSS) |

## Release-gate assessment

| Gate | Evidence |
|---|---|
| Library API and CLI start real browser tasks | `examples/`, `cli.py`, E2E tests |
| Offline CI fixture covers navigate → interact → extract | E2E + extraction tests |
| Choice/Noul anomalies fail closed | `test_judgment.py` (28 cases) |
| Unknown buttons and unapproved HTTP writes fail closed | `judgment.py`, `policy.py`, `session.py`, `test_policy_and_typing.py`, `test_e2e_local.py` |
| Independent verifier prevents false success | `test_verifier.py` + E2E |
| Unknown URL query/fragment/userinfo and opaque tokens are masked before provider inputs | `test_redaction.py`, `test_redaction_wiring.py` (URL heuristic is not a complete PII detector) |
| Global deadlines, estimator fail-closed, cancel/timeout/unknown-effect safety | `test_runtime.py`, `test_provider_budgets.py`, `test_security.py`, `test_audit_followups.py` |
| Loopback default-deny, action-scoped unsafe methods, redirect and subresource policy | `test_security.py`, `test_scope.py`, `test_audit_followups.py` (application guard only; not an egress firewall) |
| One network policy per Context; WebSocket messages default-deny | `session.py`, `test_audit_followups.py`, `test_e2e_audit_followups.py` |
| Link dedupe on raw URL identity, masked URLs only to the model | `action_space.py`, `snapshot.py`, `test_audit_followups.py`, `test_e2e_audit_followups.py` |
| Single-fetch Scrapling extraction with evidence | `test_extraction.py` |
| Scope limits and cost-estimator billing caveat documented | `docs/limits.md` |
| Reproducible benchmark recorded | `.artifacts/benchmark.json` (scripted local fixture only) |
| No unmeasured performance claim | README + benchmark `not measured` field |

**Browser-CI availability incident:** run `43040016245` (`3aa9a1e`) passed its
quality/offline and package jobs, but both Chromium jobs failed during
`python -m playwright install --with-deps chromium`: the hosted runner could not
resolve `archive.ubuntu.com` / `security.ubuntu.com` (104 `Temporary failure
resolving` errors across three attempts). No browser test started, so that run
is neither evidence for nor against the commit. The same fixes passed locally
with required mode enabled (18 browser tests). Re-run when runner DNS recovers
to obtain hosted browser evidence for this head.

**Remaining release gaps:** the upstream TypeScript comparison and paid live
smoke are unmeasured here. Follow-up audit CI run `42847672732` on `e0ca163`
passed every job, with both required Chromium jobs reporting 18 passed and no
skips; run `42847147775` on `41e1898` had passed Chromium and package jobs but
failed only the Ruff format check, which `e0ca163` fixed. Cost control fails
closed on unknown estimates but is not a
provider-enforced invoice cap. The Python network guard remains best-effort and
is not an egress firewall; use verified external isolation before untrusted
public access. PR #1 remains Draft; these are explicit gaps, not a v1.0
completion claim.
