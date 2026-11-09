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
python -m pytest -q                                      -> 253 passed
python -m pytest -q -m "not browser"                     -> 238 passed, 15 deselected
JEV_REQUIRE_BROWSER_TESTS=1 python -m pytest -q -m browser -> 15 passed, 238 deselected; no skips
python -m ruff check --no-cache .                       -> All checks passed
python -m ruff format --check .                          -> 37 files already formatted
python -m mypy src/jev_browser_py                       -> Success: no issues found (17 files)
python -m build                                          -> sdist + wheel built from this worktree
python examples/local_task.py                           -> status success, verified True (3 actions)
```

**Historical CI correction:** GitHub Actions run `42648478807` reported green on
prior HEAD `04939bc`, but its browser job logged 12 skipped and 219 deselected.
That run is not evidence that browser tests executed. The workflow now uses the
same absolute browser path for installation and testing, disables skip marks in
required mode, and fails if no browser test passes. A fresh CI run for this
worktree is pending push.

Browser tests (real Chromium, loopback fixtures, scripted judgment): 15 passed
locally with required mode enabled. They include the verified search path, a
same-origin JavaScript POST blocked for a read link, an explicitly authorized
POST form, redirects/subresources, and local page-scope behavior; no public site
is used.

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
| PYJEV-T08 | `test_runtime.py` (global awaited-stage deadlines, cancel, stuck) |
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
| Global deadlines, estimator fail-closed, cancel/timeout/unknown-effect safety | `test_runtime.py`, `test_security.py` |
| Loopback default-deny, action-scoped unsafe methods, redirect and subresource policy | `test_security.py`, `test_scope.py` (application guard only; not an egress firewall) |
| Single-fetch Scrapling extraction with evidence | `test_extraction.py` |
| Scope limits and cost-estimator billing caveat documented | `docs/limits.md` |
| Reproducible benchmark recorded | `.artifacts/benchmark.json` (scripted local fixture only) |
| No unmeasured performance claim | README + benchmark `not measured` field |

**Remaining release gaps:** the upstream TypeScript comparison and paid live
smoke are unmeasured here; a fresh GitHub Actions run for this remediation is
pending push. Cost control fails closed on unknown estimates but is not a
provider-enforced invoice cap. The Python network guard remains best-effort and
is not an egress firewall; use verified external isolation before untrusted
public access. PR #1 remains Draft; these are explicit gaps, not a v1.0
completion claim.
