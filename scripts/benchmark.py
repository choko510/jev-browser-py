"""Local benchmark and upstream-comparison harness (PR-09).

What this measures, and what it refuses to claim:

* It measures this library's own overheads on the *local* fixture with a
  scripted transport, so the numbers describe DOM observation, action
  execution, and the loop, not model latency.
* It cannot compare against the TypeScript upstream without that project
  installed and an API key, so it reports the comparison as "not measured"
  rather than inventing a ratio.

Run: ``python scripts/benchmark.py``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from fixture_server import fixture_server  # noqa: E402
from jev_browser_py.contracts import RunLimits, RunStatus  # noqa: E402
from jev_browser_py.executor import SingleActionExecutor  # noqa: E402
from jev_browser_py.runtime import RunOptions, run  # noqa: E402
from jev_browser_py.session import NetworkPolicy, open_session  # noqa: E402
from jev_browser_py.snapshot import Snapshotter  # noqa: E402
from jev_browser_py.transport import TransportReply  # noqa: E402
from jev_browser_py.verifier import RuleBasedVerifier  # noqa: E402

RESULTS = REPO / ".artifacts" / "benchmark.json"


class FixedTransport:
    """One-action-per-step scripted transport for overhead measurement."""

    name = "benchmark"

    def estimate_max_cost_usd(self, request: Mapping[str, object]) -> float:
        del request
        return 0.0

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
        del state, deadline
        self.calls += 1
        criteria = dict(questions["action"].criteria)  # type: ignore[index]
        action = next(
            (key for key in criteria if key.startswith(("search_", "click_"))),
            "scroll_down",
        )
        others = [key for key in criteria if key != action]
        probabilities = dict.fromkeys(others, 0.0)
        probabilities[action] = 1.0
        return TransportReply(
            answers={
                "action": {
                    "type": "choice",
                    "choice": action,
                    "confidence": 0.9,
                    "probabilities": probabilities,
                },
                "goal_done": {"type": "noul", "noul": 0.01},
                "stuck": {"type": "noul", "noul": 0.01},
            },
            model="benchmark-fixed",
            provider="benchmark",
            input_tokens=200,
            output_tokens=10,
        )


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}
    ordered = sorted(values)
    index95 = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[index95],
        "mean": statistics.fmean(ordered),
        "max": ordered[-1],
    }


async def measure_snapshot_overhead(iterations: int) -> list[float]:
    from playwright.async_api import async_playwright

    samples: list[float] = []
    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            try:
                snapshotter = Snapshotter(page)
                for _ in range(iterations):
                    started = time.perf_counter()
                    await snapshotter.observe()
                    samples.append(time.perf_counter() - started)
            finally:
                await browser.close()
    return samples


async def measure_full_run() -> dict[str, Any]:
    from playwright.async_api import async_playwright

    with fixture_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base + "/")
            session = await open_session(page=page, network=NetworkPolicy(allow_loopback=True))

            def picker(call: int, criteria: dict[str, str]) -> str:
                if call == 1:
                    return next(key for key in criteria if key.startswith("search_"))
                if call == 2:
                    return next(
                        key
                        for key, description in criteria.items()
                        if key.startswith("submit_") and "submit the search now" in description
                    )
                if call == 3:
                    return next(
                        key for key in criteria if key.startswith("click_") and "Annual Report 2026" in criteria[key]
                    )
                return "done"

            class PickerTransport(FixedTransport):
                def __init__(self) -> None:
                    super().__init__()

                async def judge(self, *, state: object, questions: object, deadline: float) -> TransportReply:
                    del state, deadline
                    self.calls += 1
                    criteria = dict(questions["action"].criteria)  # type: ignore[index]
                    action = picker(self.calls, criteria)
                    others = [key for key in criteria if key != action]
                    probabilities = dict.fromkeys(others, 0.0)
                    probabilities[action] = 1.0
                    return TransportReply(
                        answers={
                            "action": {
                                "type": "choice",
                                "choice": action,
                                "confidence": 0.9,
                                "probabilities": probabilities,
                            },
                            "goal_done": {"type": "noul", "noul": 0.01},
                            "stuck": {"type": "noul", "noul": 0.01},
                        },
                        model="benchmark-fixed",
                        provider="benchmark",
                        input_tokens=200,
                        output_tokens=10,
                    )

            transport = PickerTransport()
            started = time.perf_counter()
            try:
                result = await run(
                    'find the phrase "revenue of 42 million"',
                    transport=transport,
                    snapshotter=Snapshotter(page),
                    executor=SingleActionExecutor(page),
                    verifier=RuleBasedVerifier(),
                    limits=RunLimits(max_steps=6, max_seconds=60, max_jev_calls=8),
                    options=RunOptions(),
                )
            finally:
                elapsed = time.perf_counter() - started
                await session.close()
                await browser.close()
    return {
        "status": result.status.value,
        "verified": result.verified,
        "steps": result.steps,
        "jev_calls": result.jev_calls,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "elapsed_seconds": round(elapsed, 4),
        "elapsed_ok": result.status is RunStatus.SUCCESS,
    }


def _rss_mb() -> float | None:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except (ImportError, AttributeError):
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return float(counters.PeakWorkingSetSize) / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001 - diagnostics must never fail a benchmark
        return None


async def main(iterations: int) -> int:
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("note: TYPESAFE_API_KEY is unset; only scripted-transport timings are produced")
    snapshot_samples = await measure_snapshot_overhead(iterations)
    run_summary = await measure_full_run()
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "conditions": {
            "transport": "scripted, zero network latency",
            "pages": "local loopback fixture only",
            "browser": "chromium headless",
            "iterations": iterations,
        },
        "snapshot_ms": _percentiles([value * 1000 for value in snapshot_samples]),
        "full_run": run_summary,
        "peak_rss_mb": _rss_mb(),
        "upstream_comparison": {
            "status": "not measured",
            "reason": (
                "the TypeScript Jev Browser is not installed here and a live model "
                "call needs an API key; reporting a ratio would be fabricated"
            ),
        },
        "claims": "Only the numbers above were measured. No speed or cost advantage is claimed.",
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if run_summary["elapsed_ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.iterations)))
