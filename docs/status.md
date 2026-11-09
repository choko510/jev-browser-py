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
python -m pytest -q                          -> 231 passed
python -m pytest -q -m "not browser"         -> 219 passed, 12 deselected
python -m pytest -q -m browser               -> 12 passed
python -m ruff check .                       -> All checks passed
python -m ruff format --check .              -> 37 files already formatted
python -m mypy src/jev_browser_py            -> Success: no issues found (17 files)
python -m build                              -> sdist + wheel built after remediation
clean-venv wheel install/import               -> not re-run after remediation; CI package job is pending
python examples/local_task.py                -> status success, verified True (3 actions)
python scripts/benchmark.py --iterations 5   -> updated .artifacts/benchmark.json (3 actions)
```

Browser tests (real Chromium, loopback fixtures, scripted judgment): the E2E
search now fills, explicitly submits a GET form, opens the result, and verifies
the detail phrase. Scope tests include real redirected navigation and subresource
requests where unlisted hosts are mapped back to loopback; no public site is used.

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
| PYJEV-T08 | `test_runtime.py` (deadline, cancel, stuck) |
| PYJEV-T09 | `test_runtime.py`, `test_security.py` (terminal error, no retry) |
| PYJEV-T10 | `test_redaction.py`, `test_redaction_wiring.py`, `test_security.py` |
| PYJEV-T11 | `test_extraction.py` (socket-failing extraction, provenance) |
| PYJEV-T12 | `test_security.py`, `test_scope.py` (private IP, scheme, cross-origin) |
| PYJEV-T13 | `test_runtime.py` (`UNKNOWN_EFFECT`, no automatic resend) |
| PYJEV-T14 | `test_security.py` (separate sessions, locks, pages) |
| PYJEV-T15 | `test_scope.py` (SPA, iframe, canvas, popup) |
| PYJEV-T16 | `test_e2e_local.py` (search → list → detail → verified) |
| PYJEV-T17 | `scripts/benchmark.py` (p50/p95, tokens, elapsed, RSS) |

## Release-gate assessment

| Gate | Evidence |
|---|---|
| Library API and CLI start real browser tasks | `examples/`, `cli.py`, E2E tests |
| Offline CI fixture covers navigate → interact → extract | E2E + extraction tests |
| Choice/Noul anomalies fail closed | `test_judgment.py` (28 cases) |
| Unknown buttons and POST/unknown submits fail closed | `judgment.py`, `policy.py`, `test_policy_and_typing.py` |
| Independent verifier prevents false success | `test_verifier.py` + E2E |
| No critical secret leak path found | redaction suite + wiring regression test |
| Cancel/timeout/unknown-effect safety | `test_runtime.py`, `test_security.py` |
| HTTP allow-list, redirect, and subresource policy | `test_security.py`, `test_scope.py` (application guard only; not an egress firewall) |
| Single-fetch Scrapling extraction with evidence | `test_extraction.py` |
| Scope limits documented | `docs/limits.md` |
| Reproducible benchmark recorded | `.artifacts/benchmark.json` (scripted local fixture only) |
| No unmeasured performance claim | README + benchmark `not measured` field |

**Remaining release gaps:** the upstream TypeScript comparison and paid live
smoke are unmeasured here. The Python network guard remains best-effort and is
not an egress firewall; use verified external isolation before untrusted public
access. These are explicit gaps, not a v1.0 completion claim.
