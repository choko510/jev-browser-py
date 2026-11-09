"""Optional paid live smoke: one goal against a real site with a hard budget.

Requires TYPESAFE_API_KEY and a site you are permitted to automate. Run from
the manual GitHub workflow, never from CI:

    python scripts/live_smoke.py "find the page title" https://example.com/
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jev_browser_py.contracts import RunLimits  # noqa: E402
from jev_browser_py.executor import SingleActionExecutor  # noqa: E402
from jev_browser_py.policy import default_policy  # noqa: E402
from jev_browser_py.redaction import build_redactor  # noqa: E402
from jev_browser_py.runtime import RunOptions, run  # noqa: E402
from jev_browser_py.session import NetworkPolicy, open_session  # noqa: E402
from jev_browser_py.snapshot import Snapshotter  # noqa: E402
from jev_browser_py.telemetry import TraceStore, run_summary  # noqa: E402
from jev_browser_py.transport import TypeSafeTransport  # noqa: E402
from jev_browser_py.typing import RuleBasedTextGenerator  # noqa: E402
from jev_browser_py.verifier import RuleBasedVerifier  # noqa: E402


async def main(goal: str, start_url: str) -> int:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is required for the live smoke", file=sys.stderr)
        return 2
    from urllib.parse import urlsplit

    parts = urlsplit(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    network = NetworkPolicy(allowed_origins=(origin,), allow_private=False)
    redactor = build_redactor([api_key])
    transport = TypeSafeTransport(api_key=api_key)
    session = await open_session(start_url=start_url, network=network, headless=True)
    try:
        result = await run(
            goal,
            transport=transport,
            snapshotter=Snapshotter(session.page, redactor=redactor),
            executor=SingleActionExecutor(session.page),
            policy=default_policy(),
            verifier=RuleBasedVerifier(),
            limits=RunLimits(max_steps=6, max_seconds=90, max_jev_calls=8, max_cost_usd=0.05),
            options=RunOptions(),
            text_generator=RuleBasedTextGenerator(),
        )
        report = run_summary(result).as_dict()
        store = TraceStore(REPO / ".artifacts", redactor=redactor)
        path = store.write_json(report, name="live-smoke.json")
        print(json.dumps({**report, "report_path": str(path)}, indent=2))
        return 0
    finally:
        await transport.aclose()
        await session.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2])))
