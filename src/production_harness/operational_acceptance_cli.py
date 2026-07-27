"""CLI for deterministic Operational Acceptance evaluation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .operational_acceptance import (
    AcceptanceError,
    AcceptanceGate,
    build_acceptance_contract,
    evaluate_acceptance,
    load_acceptance_contract,
    load_gate_result,
    write_acceptance_contract,
    write_acceptance_report,
)


def _load_array(path: Path) -> list[object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON array in {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="operational-acceptance")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-contract", help="build a digest-bound contract")
    build.add_argument("--subject-id", required=True)
    build.add_argument("--gates-json", type=Path, required=True)
    build.add_argument("--failed-optional", choices=("fail", "ignore"), default="fail")
    build.add_argument("--output", type=Path, required=True)

    evaluate = sub.add_parser("evaluate", help="evaluate gate result files")
    evaluate.add_argument("--contract", type=Path, required=True)
    evaluate.add_argument("--result", type=Path, action="append", default=[])
    evaluate.add_argument("--evidence-root", type=Path)
    evaluate.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-contract":
            raw_gates = _load_array(args.gates_json)
            gates = [
                AcceptanceGate.from_mapping(item)
                for item in raw_gates
                if isinstance(item, dict)
            ]
            if len(gates) != len(raw_gates):
                raise ValueError("gates must be JSON objects")
            contract = build_acceptance_contract(
                subject_id=args.subject_id,
                gates=gates,
                failed_optional=args.failed_optional,
            )
            write_acceptance_contract(args.output, contract)
            print(json.dumps(contract.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0

        contract = load_acceptance_contract(args.contract)
        results = [load_gate_result(path, contract=contract) for path in args.result]
        report = evaluate_acceptance(
            contract,
            results,
            evidence_root=args.evidence_root,
        )
        write_acceptance_report(args.output, report)
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except (AcceptanceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"error": str(exc), "status": "rejected"}, sort_keys=True),
            file=sys.stderr,
        )
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
