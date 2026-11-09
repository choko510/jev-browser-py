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
deliberate: the Python-side network guard is not an egress firewall.

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
- `run()` serializes a Page through a shared per-Page lock. Freshness is checked against
  the exact proposal snapshot and a fresh live fingerprint immediately before dispatch.
  Direct `SingleActionExecutor.execute_one()` callers must supply `current_snapshot` and
  coordinate that same Page lock themselves.
- A click or submit that fails *after* it may have reached the server becomes `UNCERTAIN`
  and the run stops without retrying (`RunStatus.UNKNOWN_EFFECT`).
- Password fields are never offered unless a credential source is active; sensitive field
  names (`password`, `email`, `credit card`, ...) never receive generated text.
- Configured secrets are redacted in every representation (raw, HTML-escaped, URL-encoded,
  JSON-escaped) before page text reaches the model or a trace. Redaction failure stops the
  run.
- An injected page is never closed; only self-created browser/context/page objects are.

**What the code does *not* enforce**

- **The Python network guard is best-effort.** Context routing filters observed HTTP and
  WebSocket traffic and blocks redirects, but browser preconnects or platform-level traffic
  may bypass it. `NetworkPolicy` is not an egress firewall. Injected contexts may already
  have service workers; their context-level WebSocket route remains until that Context is
  closed. Use a dedicated Context and real isolation (container, proxy, or host firewall)
  for untrusted goals.
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
| `python -m pytest -q` (all) | 231 passed |
| `python -m pytest -q -m "not browser"` (offline) | 219 passed, 12 deselected |
| `python -m pytest -q -m browser` | 12 passed |
| `python -m ruff check .` | clean |
| `python -m ruff format --check .` | clean (37 files) |
| `python -m mypy src/jev_browser_py` | clean (17 files) |
| `python -m build` | sdist + wheel built; clean-venv import awaits the PR CI package job |
| `python examples/local_task.py` | `success`, `verified: True` (3 actions) |
| `python scripts/benchmark.py --iterations 5` | snapshot p50 ≈ 4.5 ms, full local run ≈ 0.52 s (3 actions) |

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
python -m pytest -q tests/test_e2e_local.py
```

CI runs lint, format check, mypy, offline tests on Python 3.12 and 3.13, the local
Chromium E2E, and a wheel build. The paid smoke is a separate manual workflow so no pull
request can spend money.

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
