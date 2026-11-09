"""Command-line interface.

Safety posture for the standalone CLI:

* It refuses to run without an explicit `--allow-origin`. There is no
  implicit "browse anywhere" mode, because the Python-side network guard is
  not an egress firewall (see `session.NetworkPolicy`).
* It opens its own browser and closes it, even on failure.
* It prints a redacted JSON report; page content is only written when the
  caller passes `--out`.
* It never reads an API key from a flag or prints one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .contracts import RunLimits, RunStatus
from .extraction import SelectorSpec, extract_structured
from .policy import ActionPolicy, default_policy
from .redaction import build_redactor
from .runtime import RunOptions, run
from .session import NetworkPolicy, open_session
from .snapshot import Snapshotter
from .telemetry import TraceStore, run_summary
from .transport import TypeSafeTransport
from .typing import OpenAICompatibleTextGenerator, RuleBasedTextGenerator
from .verifier import RuleBasedVerifier

__all__ = ["build_parser", "main", "run_cli"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-browser",
        description=(
            "Run one natural-language browser goal under a finite budget. "
            "Judgment is advisory; only the independent verifier can report success."
        ),
    )
    parser.add_argument("goal", help="the natural-language task to attempt")
    parser.add_argument("--url", required=True, help="start URL (http/https)")
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="an additional allowed origin, e.g. https://example.com (repeatable)",
    )
    parser.add_argument("--allow-private", action="store_true", help="allow private IP targets")
    parser.add_argument("--allow-external-write", action="store_true", help="approve submit-like actions")
    parser.add_argument("--allow-ambiguous", action="store_true", help="approve ambiguous actions")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", dest="headless", action="store_false")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-seconds", type=float, default=120.0)
    parser.add_argument("--max-jev-calls", type=int, default=20)
    parser.add_argument("--max-cost", type=float, default=0.25)
    parser.add_argument("--model", default=os.environ.get("JEV_MODEL") or None)
    parser.add_argument(
        "--typing-base-url",
        default=os.environ.get("JEV_TYPING_BASE_URL") or None,
        help="OpenAI-compatible endpoint used to generate field text",
    )
    parser.add_argument("--typing-model", default=os.environ.get("JEV_TYPING_MODEL") or None)
    parser.add_argument(
        "--extract",
        action="append",
        default=[],
        metavar="NAME=CSS",
        help="extract a named CSS value from the final HTML (repeatable)",
    )
    parser.add_argument("--out", type=Path, default=None, help="directory for a redacted report")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


async def run_cli(args: argparse.Namespace) -> int:
    if not args.allow_origin and not args.allow_private:
        print(
            "refusing to run: pass --allow-origin for every host this task may reach "
            "(the standalone network guard is not an egress firewall)",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is not set; the judgment transport cannot be built", file=sys.stderr)
        return 2
    network = NetworkPolicy(
        allowed_origins=tuple(args.allow_origin),
        allow_private=bool(args.allow_private),
    )
    policy: ActionPolicy = default_policy(
        allow_ambiguous=bool(args.allow_ambiguous),
        allow_external_write=bool(args.allow_external_write),
    )
    generator: Any
    if args.typing_base_url and args.typing_model:
        generator = OpenAICompatibleTextGenerator(
            base_url=args.typing_base_url,
            model=args.typing_model,
            api_key=os.environ.get("JEV_TYPING_API_KEY") or "",
        )
    else:
        generator = RuleBasedTextGenerator()
    transport = TypeSafeTransport(api_key=api_key, model=args.model)
    limits = RunLimits(
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        max_jev_calls=args.max_jev_calls,
        max_cost_usd=args.max_cost,
    )
    redactor = build_redactor([value for value in (api_key,) if value])
    session = await open_session(start_url=args.url, network=network, headless=args.headless)
    try:
        snapshotter = Snapshotter(session.page, redactor=redactor)
        executor = _build_executor(session.page)
        result = await run(
            args.goal,
            transport=transport,
            snapshotter=snapshotter,
            executor=executor,
            policy=policy,
            verifier=RuleBasedVerifier(),
            limits=limits,
            options=RunOptions(),
            text_generator=generator,
        )
        report = run_summary(result).as_dict()
        if args.extract:
            specs = [_parse_selector_spec(value) for value in args.extract]
            html = await session.page.content()
            extraction = extract_structured(html, url=result.final_url, specs=specs)
            report["extraction"] = {name: list(values) for name, values in extraction.values.items()}
            report["source_sha256"] = extraction.source_sha256
        if args.out is not None:
            store = TraceStore(args.out, redactor=redactor)
            path = store.write_json(report, name="report.json")
            report["report_path"] = str(path)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"status: {report['status']} (verified={report['verified']})")
            print(f"reason: {report['reason']}")
            print(f"steps={report['steps']} jev_calls={report['jev_calls']} cost=${report['estimated_cost_usd']:.6f}")
        return 0 if result.status is RunStatus.SUCCESS else 1
    finally:
        await transport.aclose()
        await session.close()


def _parse_selector_spec(value: str) -> SelectorSpec:
    name, _, selector = value.partition("=")
    if not selector:
        raise SystemExit(f"--extract expects NAME=CSS, got {value!r}")
    return SelectorSpec(name=name, selector=selector)


def _build_executor(page: Any) -> Any:
    from .executor import SingleActionExecutor

    return SingleActionExecutor(page)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
