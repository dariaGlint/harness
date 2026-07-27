from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from production_harness.evidence_ledger import (
    EvidenceVerificationError,
    LedgerLockError,
    LedgerValidationError,
    StaleLedgerError,
    append_ledger_event,
    create_evidence_reference,
    load_ledger_events,
    normalize_evidence_path,
    verify_ledger,
    verify_ledger_against_snapshot,
    write_ledger_snapshot,
)

FIXED_TIME = datetime(2026, 7, 27, 7, 30, 0, tzinfo=timezone.utc)


class EvidenceLedgerTests(unittest.TestCase):
    def test_append_verify_and_snapshot_with_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            (evidence_root / "report.json").write_text('{"ok":true}\n', encoding="utf-8")
            ref = create_evidence_reference(evidence_root, "report.json", role="report")
            ledger = root / "ledger.jsonl"
            first = append_ledger_event(
                ledger,
                event_type="task.started",
                subject_id="task-1",
                payload={"step": 1},
                actor="runner",
                timestamp=FIXED_TIME,
            )
            second = append_ledger_event(
                ledger,
                event_type="task.verified",
                subject_id="task-1",
                payload={"result": "PASS"},
                evidence=[ref],
                actor="verifier",
                timestamp=FIXED_TIME,
                expected_sequence=2,
                expected_previous_hash=first.event_hash,
            )
            self.assertEqual(second.previous_hash, first.event_hash)
            result = verify_ledger(
                ledger,
                evidence_root=evidence_root,
                expected_subject_id="task-1",
                required_event_types=("task.started", "task.verified"),
            )
            self.assertEqual(result.event_count, 2)
            self.assertEqual(result.last_hash, second.event_hash)
            self.assertEqual(result.verified_evidence_count, 1)
            snapshot = root / "snapshot.json"
            write_ledger_snapshot(ledger, snapshot)
            self.assertEqual(json.loads(snapshot.read_text())["last_hash"], second.event_hash)

    def test_hash_is_deterministic_for_identical_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hashes = []
            for name in ("a.jsonl", "b.jsonl"):
                event = append_ledger_event(
                    root / name,
                    event_type="step.completed",
                    subject_id="same",
                    payload={"b": 2, "a": 1},
                    timestamp=FIXED_TIME,
                )
                hashes.append(event.event_hash)
            self.assertEqual(hashes[0], hashes[1])

    def test_tamper_reorder_delete_insert_and_partial_records_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.jsonl"
            for index in range(1, 4):
                append_ledger_event(
                    source,
                    event_type="step.recorded",
                    subject_id="task",
                    payload={"index": index},
                    timestamp=FIXED_TIME,
                )
            lines = source.read_bytes().splitlines(keepends=True)
            cases = {
                "tamper": lines[0].replace(b'"index":1', b'"index":9') + b"".join(lines[1:]),
                "reorder": lines[1] + lines[0] + lines[2],
                "delete": lines[0] + lines[2],
                "insert": lines[0] + lines[0] + lines[1] + lines[2],
                "partial": b"".join(lines)[:-1],
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    target = root / f"{name}.jsonl"
                    target.write_bytes(content)
                    with self.assertRaises(LedgerValidationError):
                        verify_ledger(target)

    def test_stale_sequence_previous_hash_and_subject_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ledger = Path(raw) / "ledger.jsonl"
            first = append_ledger_event(
                ledger,
                event_type="task.started",
                subject_id="task",
                payload={},
                timestamp=FIXED_TIME,
            )
            with self.assertRaises(StaleLedgerError):
                append_ledger_event(
                    ledger,
                    event_type="task.continued",
                    subject_id="task",
                    payload={},
                    expected_sequence=1,
                )
            with self.assertRaises(StaleLedgerError):
                append_ledger_event(
                    ledger,
                    event_type="task.continued",
                    subject_id="task",
                    payload={},
                    expected_previous_hash="0" * 64,
                )
            with self.assertRaises(StaleLedgerError):
                append_ledger_event(
                    ledger,
                    event_type="task.continued",
                    subject_id="other",
                    payload={},
                    expected_previous_hash=first.event_hash,
                )

    def test_existing_lock_fails_closed_without_modifying_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ledger = Path(raw) / "ledger.jsonl"
            lock = Path(f"{ledger}.lock")
            lock.write_text("owned\n", encoding="utf-8")
            with self.assertRaises(LedgerLockError):
                append_ledger_event(
                    ledger,
                    event_type="task.started",
                    subject_id="task",
                    payload={},
                )
            self.assertFalse(ledger.exists())

    def test_changed_or_missing_evidence_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            evidence = evidence_root / "result.txt"
            evidence.write_text("pass\n", encoding="utf-8")
            ref = create_evidence_reference(evidence_root, "result.txt", role="result")
            ledger = root / "ledger.jsonl"
            append_ledger_event(
                ledger,
                event_type="task.verified",
                subject_id="task",
                payload={},
                evidence=[ref],
                timestamp=FIXED_TIME,
            )
            evidence.write_text("changed\n", encoding="utf-8")
            with self.assertRaises(EvidenceVerificationError):
                verify_ledger(ledger, evidence_root=evidence_root)
            evidence.unlink()
            with self.assertRaises(EvidenceVerificationError):
                verify_ledger(ledger, evidence_root=evidence_root)

    def test_unsafe_paths_and_noncanonical_json_are_rejected(self) -> None:
        for value in ("../x", "/absolute", "a\\b", "a/./b", ""):
            with self.subTest(value=value), self.assertRaises(LedgerValidationError):
                normalize_evidence_path(value)
        with tempfile.TemporaryDirectory() as raw:
            ledger = Path(raw) / "ledger.jsonl"
            event = append_ledger_event(
                ledger,
                event_type="task.started",
                subject_id="task",
                payload={},
                timestamp=FIXED_TIME,
            )
            value = event.to_dict()
            ledger.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            with self.assertRaises(LedgerValidationError):
                load_ledger_events(ledger)

    def test_cli_append_and_verify_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = root / "ledger.jsonl"
            payload = root / "payload.json"
            payload.write_text('{"status":"PASS"}\n', encoding="utf-8")
            env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            append = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "production_harness.evidence_ledger_cli",
                    "append",
                    "--ledger",
                    str(ledger),
                    "--event-type",
                    "task.completed",
                    "--subject-id",
                    "task",
                    "--payload-json",
                    str(payload),
                    "--expected-sequence",
                    "1",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(append.returncode, 0, append.stderr)
            verify = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "production_harness.evidence_ledger_cli",
                    "verify",
                    "--ledger",
                    str(ledger),
                    "--subject-id",
                    "task",
                    "--require-event",
                    "task.completed",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(json.loads(verify.stdout)["event_count"], 1)

    def test_complete_tail_truncation_requires_a_trusted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = root / "ledger.jsonl"
            snapshot = root / "snapshot.json"
            append_ledger_event(
                ledger,
                event_type="task.started",
                subject_id="task",
                payload={},
                timestamp=FIXED_TIME,
            )
            append_ledger_event(
                ledger,
                event_type="task.completed",
                subject_id="task",
                payload={},
                timestamp=FIXED_TIME,
            )
            write_ledger_snapshot(ledger, snapshot)
            lines = ledger.read_bytes().splitlines(keepends=True)
            ledger.write_bytes(lines[0])
            self.assertEqual(verify_ledger(ledger).event_count, 1)
            with self.assertRaisesRegex(LedgerValidationError, "event count"):
                verify_ledger_against_snapshot(ledger, snapshot)

    def test_empty_ledgers_and_non_finite_payloads_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ledger = Path(raw) / "ledger.jsonl"
            ledger.write_bytes(b"")
            with self.assertRaisesRegex(LedgerValidationError, "empty"):
                verify_ledger(ledger)
            self.assertEqual(verify_ledger(ledger, allow_empty=True).event_count, 0)
            with self.assertRaises(LedgerValidationError):
                append_ledger_event(
                    ledger,
                    event_type="task.started",
                    subject_id="task",
                    payload={"invalid": float("nan")},
                )

    def test_evidence_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            target = evidence_root / "target.txt"
            target.write_text("value\n", encoding="utf-8")
            link = evidence_root / "link.txt"
            try:
                link.symlink_to(target.name)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaisesRegex(EvidenceVerificationError, "symlink"):
                create_evidence_reference(evidence_root, "link.txt", role="result")

    def test_post_append_verification_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ledger = Path(raw) / "ledger.jsonl"
            with mock.patch(
                "production_harness.evidence_ledger.load_ledger_events",
                side_effect=[(), LedgerValidationError("synthetic verification failure")],
            ):
                with self.assertRaisesRegex(
                    LedgerValidationError, "synthetic verification failure"
                ):
                    append_ledger_event(
                        ledger,
                        event_type="task.started",
                        subject_id="task",
                        payload={},
                        timestamp=FIXED_TIME,
                    )
            self.assertFalse(ledger.exists())

    def test_cli_rejection_uses_structured_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = root / "payload.json"
            payload.write_text("[]\n", encoding="utf-8")
            env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "production_harness.evidence_ledger_cli",
                    "append",
                    "--ledger",
                    str(root / "ledger.jsonl"),
                    "--event-type",
                    "task.started",
                    "--subject-id",
                    "task",
                    "--payload-json",
                    str(payload),
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 10)
            self.assertEqual(json.loads(result.stderr)["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
