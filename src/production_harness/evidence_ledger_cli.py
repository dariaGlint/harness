"""Command-line interface for the append-only Evidence Ledger."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evidence_ledger import (
    EvidenceReference,
    LedgerError,
    append_ledger_event,
    create_evidence_reference,
    verify_ledger,
    verify_ledger_against_snapshot,
    write_ledger_snapshot,
)


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _evidence_specs(
    values: list[str],
    root: Path | None,
) -> list[EvidenceReference]:
    if values and root is None:
        raise ValueError("--evidence-root is required when --evidence is used")
    result: list[EvidenceReference] = []
    for value in values:
        if "=" not in value:
            raise ValueError("--evidence must use ROLE=RELATIVE_PATH")
        role, relative_path = value.split("=", 1)
        assert root is not None
        result.append(
            create_evidence_reference(root, relative_path, role=role)
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evidence-ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="append one verified ledger event")
    append.add_argument("--ledger", type=Path, required=True)
    append.add_argument("--event-type", required=True)
    append.add_argument("--subject-id", required=True)
    append.add_argument("--payload-json", type=Path, required=True)
    append.add_argument("--actor")
    append.add_argument("--evidence-root", type=Path)
    append.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="ROLE=PATH",
    )
    append.add_argument("--expected-sequence", type=int)
    append.add_argument("--expected-previous-hash")
    append.add_argument("--snapshot", type=Path)

    verify = sub.add_parser(
        "verify",
        help="verify the entire ledger and optional evidence",
    )
    verify.add_argument("--ledger", type=Path, required=True)
    verify.add_argument("--evidence-root", type=Path)
    verify.add_argument("--subject-id")
    verify.add_argument("--require-event", action="append", default=[])
    verify.add_argument("--expected-event-count", type=int)
    verify.add_argument("--expected-last-hash")
    verify.add_argument("--snapshot", type=Path)
    verify.add_argument("--allow-empty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "append":
            event = append_ledger_event(
                args.ledger,
                event_type=args.event_type,
                subject_id=args.subject_id,
                payload=_json_object(args.payload_json),
                evidence=_evidence_specs(args.evidence, args.evidence_root),
                actor=args.actor,
                expected_sequence=args.expected_sequence,
                expected_previous_hash=args.expected_previous_hash,
            )
            if args.snapshot is not None:
                write_ledger_snapshot(args.ledger, args.snapshot)
            print(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0

        if args.snapshot is not None:
            if any(
                value is not None
                for value in (
                    args.subject_id,
                    args.expected_event_count,
                    args.expected_last_hash,
                )
            ) or args.allow_empty:
                raise ValueError(
                    "--snapshot cannot be combined with explicit head anchors"
                )
            result = verify_ledger_against_snapshot(
                args.ledger,
                args.snapshot,
                evidence_root=args.evidence_root,
                required_event_types=args.require_event,
            )
        else:
            result = verify_ledger(
                args.ledger,
                evidence_root=args.evidence_root,
                expected_subject_id=args.subject_id,
                required_event_types=args.require_event,
                expected_event_count=args.expected_event_count,
                expected_last_hash=args.expected_last_hash,
                allow_empty=args.allow_empty,
            )
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except (LedgerError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"error": str(exc), "status": "rejected"}, sort_keys=True),
            file=sys.stderr,
        )
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
