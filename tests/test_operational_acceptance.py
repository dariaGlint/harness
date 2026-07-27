from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "production_harness"
if "production_harness" not in sys.modules:
    package = types.ModuleType("production_harness")
    package.__path__ = [str(PACKAGE_ROOT)]
    package.__package__ = "production_harness"
    sys.modules["production_harness"] = package

state = importlib.import_module("production_harness.state")
evidence_ledger = importlib.import_module("production_harness.evidence_ledger")
acceptance = importlib.import_module("production_harness.operational_acceptance")
acceptance_cli = importlib.import_module("production_harness.operational_acceptance_cli")

AcceptanceEvidenceError = acceptance.AcceptanceEvidenceError
AcceptanceGate = acceptance.AcceptanceGate
AcceptanceValidationError = acceptance.AcceptanceValidationError
build_acceptance_contract = acceptance.build_acceptance_contract
build_gate_result = acceptance.build_gate_result
evaluate_acceptance = acceptance.evaluate_acceptance
verify_acceptance_contract = acceptance.verify_acceptance_contract
verify_acceptance_report = acceptance.verify_acceptance_report
verify_gate_result = acceptance.verify_gate_result
create_evidence_reference = evidence_ledger.create_evidence_reference


class OperationalAcceptanceTests(unittest.TestCase):
    def contract(self, *, failed_optional: str = "fail"):
        return build_acceptance_contract(
            subject_id="task-13",
            gates=[
                AcceptanceGate(
                    "compile",
                    required=True,
                    required_evidence_roles=("compile-report",),
                ),
                AcceptanceGate("docs", required=False),
                AcceptanceGate("tests", required=True),
            ],
            failed_optional=failed_optional,
        )

    def result(self, contract, gate_id: str, status: str = "pass", *, evidence=()):
        return build_gate_result(
            contract,
            gate_id=gate_id,
            status=status,
            reason=f"{gate_id} {status}",
            evidence=evidence,
        )

    def test_contract_is_deterministic_and_sorted(self) -> None:
        first = self.contract()
        second = build_acceptance_contract(
            subject_id="task-13",
            gates=list(reversed(first.gates)),
        )
        self.assertEqual(first.contract_sha256, second.contract_sha256)
        self.assertEqual([g.gate_id for g in first.gates], ["compile", "docs", "tests"])

    def test_complete_required_pass_set_produces_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            (evidence_root / "compile.json").write_text('{"ok":true}\n', encoding="utf-8")
            reference = create_evidence_reference(
                evidence_root, "compile.json", role="compile-report"
            )
            contract = self.contract()
            report = evaluate_acceptance(
                contract,
                [
                    self.result(contract, "tests"),
                    self.result(contract, "compile", evidence=[reference]),
                ],
                evidence_root=evidence_root,
            )
            self.assertEqual(report.task_verdict, "PASS")
            self.assertEqual(report.conformance_status, "pass")
            self.assertEqual([r.gate_id for r in report.ordered_results], ["compile", "tests"])

    def test_required_failure_produces_fail(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("tests")],
        )
        report = evaluate_acceptance(contract, [self.result(contract, "tests", "fail")])
        self.assertEqual(report.task_verdict, "FAIL")
        self.assertEqual(report.failed_gates, ("tests",))

    def test_missing_blocked_and_skipped_required_gates_produce_blocked(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("a"), AcceptanceGate("b"), AcceptanceGate("c")],
        )
        report = evaluate_acceptance(
            contract,
            [
                self.result(contract, "b", "blocked"),
                self.result(contract, "c", "skipped"),
            ],
        )
        self.assertEqual(report.task_verdict, "BLOCKED")
        self.assertEqual(report.missing_required_gates, ("a",))
        self.assertEqual(report.blocked_required_gates, ("b",))
        self.assertEqual(report.skipped_required_gates, ("c",))
        self.assertEqual(report.conformance_status, "pass")

    def test_optional_failure_policy_is_explicit(self) -> None:
        fail_contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("optional", required=False)],
            failed_optional="fail",
        )
        ignore_contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("optional", required=False)],
            failed_optional="ignore",
        )
        self.assertEqual(
            evaluate_acceptance(
                fail_contract, [self.result(fail_contract, "optional", "fail")]
            ).task_verdict,
            "FAIL",
        )
        self.assertEqual(
            evaluate_acceptance(
                ignore_contract, [self.result(ignore_contract, "optional", "fail")]
            ).task_verdict,
            "PASS",
        )

    def test_duplicate_unexpected_and_mismatched_results_are_rejected(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("tests")],
        )
        result = self.result(contract, "tests")
        with self.assertRaisesRegex(AcceptanceValidationError, "duplicate"):
            evaluate_acceptance(contract, [result, result])
        raw = result.to_dict()
        raw["gate_id"] = "unexpected"
        raw["result_sha256"] = acceptance._digest(acceptance._result_unsigned(raw))
        with self.assertRaisesRegex(AcceptanceValidationError, "unexpected"):
            verify_gate_result(raw, contract=contract)
        other = build_acceptance_contract(
            subject_id="other",
            gates=[AcceptanceGate("tests")],
        )
        with self.assertRaisesRegex(AcceptanceValidationError, "contract digest"):
            verify_gate_result(result.to_dict(), contract=other)

    def test_required_evidence_role_and_changed_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            evidence = evidence_root / "compile.json"
            evidence.write_text('{"ok":true}\n', encoding="utf-8")
            contract = build_acceptance_contract(
                subject_id="task",
                gates=[AcceptanceGate("compile", required_evidence_roles=("report",))],
            )
            with self.assertRaisesRegex(AcceptanceEvidenceError, "missing required"):
                evaluate_acceptance(contract, [self.result(contract, "compile")])
            reference = create_evidence_reference(evidence_root, "compile.json", role="report")
            result = self.result(contract, "compile", evidence=[reference])
            evidence.write_text('{"ok":false}\n', encoding="utf-8")
            with self.assertRaisesRegex(AcceptanceEvidenceError, "changed"):
                evaluate_acceptance(contract, [result], evidence_root=evidence_root)

    def test_contract_result_and_report_digest_tampering_is_rejected(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("tests")],
        )
        raw_contract = contract.to_dict()
        raw_contract["subject_id"] = "tampered"
        with self.assertRaisesRegex(AcceptanceValidationError, "digest"):
            verify_acceptance_contract(raw_contract)
        result = self.result(contract, "tests")
        raw_result = result.to_dict()
        raw_result["reason"] = "tampered"
        with self.assertRaisesRegex(AcceptanceValidationError, "digest"):
            verify_gate_result(raw_result)
        report = evaluate_acceptance(contract, [result])
        raw_report = report.to_dict()
        raw_report["task_verdict"] = "FAIL"
        with self.assertRaisesRegex(AcceptanceValidationError, "digest"):
            verify_acceptance_report(raw_report)

    def test_report_semantics_are_rechecked_against_contract(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("tests")],
        )
        result = self.result(contract, "tests")
        report = evaluate_acceptance(contract, [result])
        self.assertEqual(
            verify_acceptance_report(report.to_dict(), contract=contract), report
        )

    def test_cli_build_and_evaluate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            gates = root / "gates.json"
            contract_path = root / "contract.json"
            result_path = root / "result.json"
            report_path = root / "report.json"
            gates.write_text(
                json.dumps([AcceptanceGate("tests").to_dict()]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                acceptance_cli.main(
                    [
                        "build-contract",
                        "--subject-id",
                        "task",
                        "--gates-json",
                        str(gates),
                        "--output",
                        str(contract_path),
                    ]
                ),
                0,
            )
            contract = acceptance.load_acceptance_contract(contract_path)
            acceptance.write_gate_result(result_path, self.result(contract, "tests"))
            self.assertEqual(
                acceptance_cli.main(
                    [
                        "evaluate",
                        "--contract",
                        str(contract_path),
                        "--result",
                        str(result_path),
                        "--output",
                        str(report_path),
                    ]
                ),
                0,
            )
            self.assertEqual(json.loads(report_path.read_text())["task_verdict"], "PASS")

    def test_packaged_schemas_are_valid_json(self) -> None:
        names = (
            "operational-acceptance-contract-v1.schema.json",
            "operational-acceptance-gate-result-v1.schema.json",
            "operational-acceptance-report-v1.schema.json",
        )
        for name in names:
            with self.subTest(name=name):
                value = json.loads((PACKAGE_ROOT / "schemas" / name).read_text())
                self.assertEqual(value["properties"]["schema_version"]["const"], 1)


    def test_noncanonical_contract_and_report_order_are_rejected(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("a"), AcceptanceGate("b")],
        )
        raw_contract = contract.to_dict()
        raw_contract["gates"][0]["allowed_statuses"] = [
            "fail", "pass", "blocked", "skipped"
        ]
        raw_contract["contract_sha256"] = acceptance._digest(
            acceptance._contract_unsigned(raw_contract)
        )
        with self.assertRaisesRegex(AcceptanceValidationError, "canonically"):
            verify_acceptance_contract(raw_contract)

        first = self.result(contract, "a")
        second = self.result(contract, "b")
        report = evaluate_acceptance(contract, [first, second]).to_dict()
        report["ordered_results"] = list(reversed(report["ordered_results"]))
        report["report_sha256"] = acceptance._digest(
            acceptance._report_unsigned(report)
        )
        with self.assertRaisesRegex(AcceptanceValidationError, "sorted"):
            verify_acceptance_report(report)

    def test_missing_optional_gate_does_not_block(self) -> None:
        contract = build_acceptance_contract(
            subject_id="task",
            gates=[AcceptanceGate("optional", required=False)],
        )
        report = evaluate_acceptance(contract, [])
        self.assertEqual(report.task_verdict, "PASS")
        self.assertEqual(report.missing_required_gates, ())

    def test_invalid_identifiers_nonfinite_payload_and_unsupported_status_fail(self) -> None:
        with self.assertRaises(AcceptanceValidationError):
            build_acceptance_contract(subject_id="bad subject", gates=[AcceptanceGate("a")])
        contract = build_acceptance_contract(subject_id="task", gates=[AcceptanceGate("a")])
        with self.assertRaises(AcceptanceValidationError):
            build_gate_result(contract, gate_id="a", status="unknown", reason="bad")
        with self.assertRaises(AcceptanceValidationError):
            build_acceptance_contract(
                subject_id="task",
                gates=[AcceptanceGate("a")],
                failed_optional="block",
            )


if __name__ == "__main__":
    unittest.main()
