"""CLI for the generic production harness foreground supervisor."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .foreground import (
    EXIT_UNRECOVERABLE,
    CommandTemplate,
    ForegroundRequest,
    ForegroundSupervisorError,
    run_until_boundary,
)


def _command_template(value: str) -> CommandTemplate:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON command template: {exc}") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
        raise argparse.ArgumentTypeError("command template must be a non-empty JSON string array")
    return CommandTemplate(tuple(parsed))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--command-template-json", type=_command_template, required=True)
    parser.add_argument("--outer-time-budget", type=float, default=40.0)
    parser.add_argument("--invocation-time-budget", type=float, default=12.0)
    parser.add_argument("--max-invocations", type=int, default=16)
    parser.add_argument("--reserve-seconds", type=float, default=1.5)
    parser.add_argument("--state-filename", default="state.json")
    parser.add_argument("--state-machine-key", default="machine_state")
    parser.add_argument("--report", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("start", "resume"):
        subparser = subparsers.add_parser(operation)
        _add_common(subparser)
        subparser.add_argument("--task-id", required=True)
    latest = subparsers.add_parser("resume-latest")
    _add_common(latest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = ForegroundRequest(
            operation=args.operation,
            cwd=args.cwd,
            state_root=args.state_root,
            task_id=getattr(args, "task_id", ""),
            command_template=args.command_template_json,
            outer_time_budget=args.outer_time_budget,
            invocation_time_budget=args.invocation_time_budget,
            max_invocations=args.max_invocations,
            reserve_seconds=args.reserve_seconds,
            report_path=args.report,
            state_filename=args.state_filename,
            state_machine_key=args.state_machine_key,
        )
        return run_until_boundary(request)
    except (ForegroundSupervisorError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"PRODUCTION_HARNESS_UNRECOVERABLE: {exc}", file=sys.stderr)
        return EXIT_UNRECOVERABLE


if __name__ == "__main__":
    raise SystemExit(main())
