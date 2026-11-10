# jev-browser-py

**Python-native browser agent inspired by [Jev Browser](https://github.com/jkudish/jev-browser), with [Scrapling](https://github.com/D4Vinci/Scrapling) extraction.**

> **Status (2026-11-09):** Draft standalone implementation under review; not release-ready.
> Python 3.12+, async Playwright, the official TypeSafe Python SDK, and Scrapling are
> integrated. Safety limits and remaining verification gaps are listed in
> [docs/status.md](docs/status.md) and [docs/limits.md](docs/limits.md).

## Purpose

Take a natural-language goal, observe a web page, ask TypeSafe/Jev for three typed
judgments (`Choice(action)`, `Noul(goal_done)`, `Noul(stuck)`), and execute **at most one**
authorized action with Playwright before re-observing. Scrapling parses the page you
already have; it never fetches twice.

- **Python-first.** Python 3.12+, async Playwright, official TypeSafe SDK. Node.js Jev
  Browser is not a runtime dependency.
- **Judgment ≠ Execution.** Jev proposes, the policy authorizes, the executor performs a
  single action, and an **independent verifier** decides whether the goal was reached.
  A high confidence score or `goal_done` is never treated as success.
- **Standalone first.** This package runs, tests, and releases on its own. An aiagent-md
  adapter is a later, separate step.
- **Evidence first.** Redacted step traces, token usage, and extraction provenance
  (URL, time, selector, source SHA-256) are retained. Secrets are not.

## Install

```bash
pip install jev-browser-py
python -m playwright install chromium   # once, for the browser
```

From a checkout:

```bash
pip install -e ".[dev]"
python -m playwright install chromium
```

## Quick start (offline, no API key)

```bash
python examples/local_task.py
```

That example starts a loopback HTTP fixture, runs a real headless Chromium, uses a
scripted transport in place of the model, executes real clicks, verifies the outcome,
and extracts values with Scrapling. Expected output ends with `status: success` and
`verified: True`.

## Quick start (real Jev judgments)

```python
import asyncio, os

from jev_browser_py import TypeSafeTransport
from jev_browser_py.contracts import RunLimits
from jev_browser_py.executor import SingleActionExecutor
from jev_browser_py.policy import default_policy
from jev_browser_py.runtime import RunOptions, run
from jev_browser_py.session import NetworkPolicy, open_session
from jev_browser_py.snapshot import Snapshotter
from jev_browser_py.verifier import RuleBasedVerifier


async def main() -> None:
    transport = TypeSafeTransport(api_key=os.environ["TYPESAFE_API_KEY"])
    session = await open_session(
        start_url="https://example.com/",
        network=NetworkPolicy(allowed_origins=("https://example.com",)),
    )
    try:
        result = await run(
            'confirm the page mentions "Example Domain"',
            transport=transport,
            snapshotter=Snapshotter(session.page),
            executor=SingleActionExecutor(session.page),
            policy=default_policy(),  # read + local input only
            verifier=RuleBasedVerifier(),
            limits=RunLimits(max_steps=8, max_seconds=90, max_jev_calls=10),
            options=RunOptions(),
        )
        print(result.status, result.verified, result.reason)
    finally:
        await transport.aclose()
        await session.close()


asyncio.run(main())
```

`result.status is RunStatus.SUCCESS` means the **independent verifier** confirmed the
goal. Anything else is a classified non-success.

## CLI

```bash
export TYPESAFE_API_KEY=...
jev-browser 'find the phrase "pricing section"' https://example.com/ \
  --allow-origin https://example.com \
  --max-steps 8 --max-seconds 90 \
  --extract title=title --extract links=a \
  --out .artifacts --json
```

The CLI refuses to run without `--allow-origin` (or `--allow-private`). That refusal is
deliberate: the Python-side network guard is not an egress firewall. Loopback is
not implicit: local targets require an exact `--allow-origin`; `--allow-private`
does not enable localhost.

## Observable surface

| Module | Responsibility |
|---|---|
| `contracts.py` | Frozen, extra-free DTOs, status enums, error codes |
| `questions.py` | The typed `Choice`/`Noul` catalog and bounded model state |
| `transport.py` | `JevTransport` protocol, TypeSafe adapter, offline mocks |
| `judgment.py` | Strict fail-closed validation and proposal binding |
| `action_space.py` | Pruning, dedupe, capping, ephemeral element IDs, fingerprints |
| `snapshot.py` | Playwright observation, visible-text excerpt, redaction |
| `policy.py` | Deny-by-default authorization |
| `executor.py` | Preflight freshness, one action, effect classification |
| `runtime.py` | Bounded Observe → Judge → Act loop |
| `typing.py` | Field-text generation with validation and safe degradation |
| `verifier.py` | Independent outcome verification |
| `extraction.py` | Scrapling `Selector` over existing HTML, with provenance |
| `session.py` | Browser ownership, injected pages, network policy |
| `telemetry.py` | Redacted reports and trace storage |
| `cli.py` | The `jev-browser` command |

## Safety model

**What the code enforces**

- Judgment is a proposal. `parse_proposal` rejects any action not present in the current
  action space; the policy then denies by default.
- `search_eN` fills a query without pressing Enter. A separate `submit_eN` is the
  explicit submit proposal; only a known GET search form is read-only, while POST or
  unknown submission requires explicit policy permission. Other submit controls are
  also offered as `submit_eN`, never as a click.
- `external_write` and `ambiguous` side-effect classes require an explicit opt-in
  (`default_policy(allow_external_write=True)`). A confidence of 1.0 does not bypass this.
- Loopback is denied by default; an exact local origin must be allow-listed. Unsafe HTTP
  methods (including POST/PUT/PATCH/DELETE) are blocked unless the network policy enables
  them and an approved write/ambiguous action is currently executing. The CLI's
  `--allow-external-write` enables both policy gates; API callers must configure both.
- `run()` serializes a Page through a shared per-Page lock and refuses a real Playwright
  Page without a registered `open_session` network guard. The lock wait counts against
  `max_seconds`; a run that cannot get the Page in time is `TIMEOUT` and touches nothing.
  If a cancelled page operation does not stop within its settle window, later runs wait for it
  and then report `BLOCKED`/`UNKNOWN_EFFECT` instead of acting on a Page that may still be in use.
  Freshness is checked against the exact proposal snapshot and a fresh live fingerprint
  immediately before dispatch. Direct `SingleActionExecutor.execute_one()` callers must
  install/use a guarded session, supply `current_snapshot`, coordinate that same Page lock,
  and explicitly scope any unsafe requests themselves.
- A click or submit that fails *after* it may have reached the server becomes `UNCERTAIN`
  and the run stops without retrying (`RunStatus.UNKNOWN_EFFECT`).
- Password fields are never offered unless a credential source is active; sensitive field
  names (`password`, `email`, `credit card`, ...) never receive generated text.
- Configured secrets are redacted in every representation (raw, HTML-escaped, URL-encoded,
  JSON-escaped) before page text reaches the model or a trace. Model-facing URLs also remove
  userinfo, fragments, all query values (including unknown keys), and long opaque path tokens.
  Redaction failure stops the run. Link dedupe uses a private digest of the raw destination,
  not the masked href, so two links whose masked URLs look identical stay two actions while
  neither raw value reaches the model.
- Every provider must declare a per-call maximum with `CostEstimator.estimate_max_cost_usd`;
  missing, invalid, or over-budget estimates block the call before dispatch. `RunResult` reports
  the reserved `estimated_cost_usd`. The OpenAI-compatible typing provider needs an explicit
  per-call cap (`--typing-max-cost-per-call` on the CLI).
- An injected page is never closed; only self-created browser/context/page objects are.

**What the code does *not* enforce**

- **The Python network guard is best-effort.** Context routing filters observed HTTP and
  WebSocket traffic and blocks redirects, but browser preconnects or platform-level traffic
  may bypass it. `NetworkPolicy` is not an egress firewall. A Context enforces exactly one
  policy: the first session installs it and any other session on that Context must pass the
  same policy object or fail to install. Page-to-server WebSocket messages are dropped unless
  an authorized write action is active, because Playwright exposes no Page on a socket route.
  Injected contexts may already have service workers; their context-level WebSocket route
  remains until that Context is closed. Use a dedicated Context and real isolation (container,
  proxy, or host firewall) for untrusted goals.
- The local cost ceiling is only as reliable as each provider's declared upper-bound estimator
  and reported usage. It is not a provider-enforced financial cap: billing, hidden provider
  overhead, or already-accepted remote work can differ. Configure provider-side spending limits
  for a hard invoice cap. `max_seconds` bounds the run's awaited work, including the Page lock
  wait, and passes the remaining deadline to the typing HTTP request, but it cannot recall work
  already accepted remotely.
- `RuleBasedVerifier` verifies explicit goal phrases only. Goals without an extractable
  evidence target remain `UNVERIFIED`; use a task-specific `CallableVerifier` for
  destination or state checks.
- CAPTCHA/MFA/login walls are reported as `needs_human`/`blocked`, never bypassed.
- The current draft supports same-document actions, native selects, scrolling, and back. SPA route
  changes that keep the same DOM are handled; iframe, Shadow DOM, and popup targets are
  out of scope and stop safely rather than pretending to succeed.

## Verification

Executed in this workspace (Windows, Python 3.14.3, Playwright 1.64.1, bundled Chromium):

| Command | Result |
|---|---|
| `python -m pytest -q` (all) | 266 passed |
| `python -m pytest -q -m "not browser"` (offline) | 248 passed, 18 deselected |
| `JEV_REQUIRE_BROWSER_TESTS=1 python -m pytest -q -m browser` | 18 passed, 248 deselected; no browser skips |
| `python -m ruff check --no-cache .` | all checks passed |
| `python -m ruff format --check .` | 40 files already formatted |
| `python -m mypy src/jev_browser_py` | no issues (17 files) |
| `python -m build` | sdist + wheel built from the remediation worktree |
| `python examples/local_task.py` | `success`, `verified: True` (3 actions) |
| GitHub Actions run `42648478807` on prior HEAD `04939bc` | jobs reported green, but browser logs showed 12 skipped and 219 deselected; not accepted as browser execution evidence |
| First remediation CI `42677681232` (`aac681c`) | browser/package jobs passed; quality jobs failed only at Ruff formatting, fixed in `b4d9f5d` |
| GitHub Actions rerun `42823111903` (`b4d9f5d`) | all jobs passed; each Chromium job reported 15 passed, 238 deselected with `JEV_REQUIRE_BROWSER_TESTS=1` |
| Code/test CI `42826391344` (`9afe9a7`) | all jobs passed after test-module split; both Chromium jobs reported 15 passed, 238 deselected |
| Documentation follow-up CI `42827933717` (`9de03bb`) | all quality, required Chromium, package-build, and wheel-import jobs passed |
| Follow-up audit fixes (local) | 266 passed; offline 248; required Chromium 18 passed with no skips; Ruff, format (40 files), mypy, build, import smoke all pass |
| GitHub Actions run `42847147775` (`41e1898`) | Chromium and package jobs passed; both quality jobs failed only at Ruff format (one blank line in `runtime.py`), fixed in `e0ca163` |
| GitHub Actions run `42847672732` (`e0ca163`) | all jobs passed; both required Chromium jobs reported 18 passed, 248 deselected with `JEV_REQUIRE_BROWSER_TESTS=1` and no skips; offline jobs reported 248 passed, 12 deselected |
| `scripts/benchmark.py --iterations 5` | not rerun for this remediation; previous local measurements are historical only |

**Not executed here:** the paid TypeSafe live smoke (no API key in this environment) and
the upstream TypeScript comparison. `scripts/benchmark.py` records
`"upstream_comparison": {"status": "not measured"}` rather than fabricating a ratio.
Run `.github/workflows/live-smoke.yml` with a `TYPESAFE_API_KEY` secret to exercise the
real provider.

See [docs/status.md](docs/status.md) for the per-PR log, the PYJEV-T01..T17 mapping,
and the release-gate assessment.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m mypy src/jev_browser_py
python -m pytest -q -m "not browser"
python -m playwright install chromium
JEV_REQUIRE_BROWSER_TESTS=1 python -m pytest -q -m browser
```

CI uses the same absolute `PLAYWRIGHT_BROWSERS_PATH` for browser installation and
execution and sets `JEV_REQUIRE_BROWSER_TESTS=1`; that mode removes browser skip
markers and fails if no browser test passes. CI runs lint, format check, mypy,
offline tests on Python 3.12 and 3.13, Chromium E2E, and a wheel build. The paid
smoke is a separate manual workflow so no pull request can spend money.

## Packaging

```bash
python -m build
pip install dist/*.whl
python -c "import jev_browser_py; print(jev_browser_py.__version__)"
```

A Docker example is in [docs/docker.md](docs/docker.md).

## References

- [Jev Browser (TypeScript)](https://github.com/jkudish/jev-browser) — behaviour baseline
- [Jev Agent Tools](https://github.com/jkudish/jev-agent-tools) — validation reference
- [TypeSafe Python SDK](https://github.com/typesafe-ai/typesafe-sdk-python) — inference
- [Scrapling](https://github.com/D4Vinci/Scrapling) — HTML extraction
- [aiagent-md](https://github.com/choko510/aiagent-md) — future integration target

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for licence attribution and
[docs/limits.md](docs/limits.md) for the supported-site boundary.

## Licence

MIT. Portions of the action-space behaviour are adapted from Jev Browser (MIT); see
[LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
