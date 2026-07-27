"""Evidence verification and report evaluation for Operational Acceptance."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .evidence_ledger import (
    EvidenceReference, EvidenceVerificationError, LedgerValidationError,
    create_evidence_reference,
)
from .operational_acceptance_common import (
    ACCEPTANCE_SCHEMA_VERSION, REPORT_KIND, AcceptanceEvidenceError,
    AcceptanceValidationError, _ID_RE, _digest, _exact_fields, _report_unsigned,
    _require_sha, _require_text, _text_tuple,
)
from .operational_acceptance_contract import verify_acceptance_contract
from .operational_acceptance_result import verify_gate_result
from .operational_acceptance_types import AcceptanceContract, AcceptanceReport, GateResult
from .state import atomic_write_json

def _verify_evidence_reference(reference: EvidenceReference, root: Path) -> None:
    try:
        actual = create_evidence_reference(root, reference.path, role=reference.role)
    except (LedgerValidationError, EvidenceVerificationError) as exc:
        raise AcceptanceEvidenceError(str(exc)) from exc
    if actual.size_bytes != reference.size_bytes or actual.sha256 != reference.sha256:
        raise AcceptanceEvidenceError(f"evidence file changed: {reference.path}")


def evaluate_acceptance(
    contract: AcceptanceContract | Mapping[str, Any],
    results: Iterable[GateResult | Mapping[str, Any]],
    *,
    evidence_root: Path | None = None,
) -> AcceptanceReport:
    """Validate all results and produce one deterministic acceptance report."""
    verified_contract = (
        contract
        if isinstance(contract, AcceptanceContract)
        else verify_acceptance_contract(contract)
    )
    gates = {gate.gate_id: gate for gate in verified_contract.gates}
    by_gate: dict[str, GateResult] = {}
    for raw in results:
        result = (
            raw
            if isinstance(raw, GateResult)
            else verify_gate_result(raw, contract=verified_contract)
        )
        result = verify_gate_result(result.to_dict(), contract=verified_contract)
        if result.gate_id in by_gate:
            raise AcceptanceValidationError(f"duplicate gate result: {result.gate_id}")
        by_gate[result.gate_id] = result

    ordered: list[GateResult] = []
    missing_required: list[str] = []
    failed: list[str] = []
    blocked_required: list[str] = []
    skipped_required: list[str] = []

    for gate in verified_contract.gates:
        result = by_gate.get(gate.gate_id)
        if result is None:
            if gate.required:
                missing_required.append(gate.gate_id)
            continue
        ordered.append(result)
        roles = {reference.role for reference in result.evidence}
        missing_roles = sorted(set(gate.required_evidence_roles) - roles)
        if missing_roles:
            raise AcceptanceEvidenceError(
                f"gate {gate.gate_id} is missing required evidence roles: {missing_roles}"
            )
        if evidence_root is not None:
            for reference in result.evidence:
                _verify_evidence_reference(reference, Path(evidence_root))
        if result.status == "fail" and (
            gate.required or verified_contract.verdict_policy["failed_optional"] == "fail"
        ):
            failed.append(gate.gate_id)
        if gate.required and result.status == "blocked":
            blocked_required.append(gate.gate_id)
        if gate.required and result.status == "skipped":
            skipped_required.append(gate.gate_id)

    if failed:
        verdict = "FAIL"
    elif missing_required or blocked_required or skipped_required:
        verdict = "BLOCKED"
    else:
        verdict = "PASS"

    unsigned = {
        "blocked_required_gates": blocked_required,
        "conformance_status": "pass",
        "contract_sha256": verified_contract.contract_sha256,
        "failed_gates": failed,
        "kind": REPORT_KIND,
        "missing_required_gates": missing_required,
        "ordered_results": [item.to_dict() for item in ordered],
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "skipped_required_gates": skipped_required,
        "subject_id": verified_contract.subject_id,
        "task_verdict": verdict,
    }
    value = dict(unsigned)
    value["report_sha256"] = _digest(unsigned)
    return verify_acceptance_report(value)


def verify_acceptance_report(
    value: Mapping[str, Any],
    *,
    contract: AcceptanceContract | Mapping[str, Any] | None = None,
) -> AcceptanceReport:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "contract_sha256",
            "subject_id",
            "conformance_status",
            "task_verdict",
            "ordered_results",
            "missing_required_gates",
            "failed_gates",
            "blocked_required_gates",
            "skipped_required_gates",
            "report_sha256",
        },
        "acceptance report",
    )
    if value["schema_version"] != ACCEPTANCE_SCHEMA_VERSION or value["kind"] != REPORT_KIND:
        raise AcceptanceValidationError("unsupported acceptance report schema")
    contract_sha = _require_sha(value["contract_sha256"], "contract_sha256")
    subject = _require_text(value["subject_id"], "subject_id", pattern=_ID_RE)
    if value["conformance_status"] != "pass":
        raise AcceptanceValidationError("conformance_status must be pass for a valid report")
    verdict = value["task_verdict"]
    if verdict not in {"PASS", "FAIL", "BLOCKED"}:
        raise AcceptanceValidationError("unsupported task verdict")
    raw_results = value["ordered_results"]
    if not isinstance(raw_results, list):
        raise AcceptanceValidationError("ordered_results must be an array")
    results = tuple(
        verify_gate_result(item)
        for item in raw_results
        if isinstance(item, dict)
    )
    if len(results) != len(raw_results):
        raise AcceptanceValidationError("ordered_results entries must be objects")
    gate_ids = tuple(item.gate_id for item in results)
    if gate_ids != tuple(sorted(gate_ids)):
        raise AcceptanceValidationError("ordered_results must be sorted by gate_id")
    if len(set(gate_ids)) != len(gate_ids):
        raise AcceptanceValidationError("ordered_results must not contain duplicate gates")
    if any(item.contract_sha256 != contract_sha for item in results):
        raise AcceptanceValidationError("ordered result contract digest mismatch")
    if any(item.subject_id != subject for item in results):
        raise AcceptanceValidationError("ordered result subject mismatch")
    missing = _text_tuple(value["missing_required_gates"], "missing_required_gates")
    failed = _text_tuple(value["failed_gates"], "failed_gates")
    blocked = _text_tuple(value["blocked_required_gates"], "blocked_required_gates")
    skipped = _text_tuple(value["skipped_required_gates"], "skipped_required_gates")
    report_sha = _require_sha(value["report_sha256"], "report_sha256")
    if report_sha != _digest(_report_unsigned(value)):
        raise AcceptanceValidationError("acceptance report digest mismatch")

    report = AcceptanceReport(
        schema_version=ACCEPTANCE_SCHEMA_VERSION,
        kind=REPORT_KIND,
        contract_sha256=contract_sha,
        subject_id=subject,
        conformance_status="pass",
        task_verdict=verdict,
        ordered_results=results,
        missing_required_gates=missing,
        failed_gates=failed,
        blocked_required_gates=blocked,
        skipped_required_gates=skipped,
        report_sha256=report_sha,
    )
    if contract is not None:
        verified_contract = (
            contract
            if isinstance(contract, AcceptanceContract)
            else verify_acceptance_contract(contract)
        )
        recomputed = evaluate_acceptance(verified_contract, results)
        if report.to_dict() != recomputed.to_dict():
            raise AcceptanceValidationError("acceptance report does not match contract semantics")
    return report


def write_acceptance_contract(path: Path, contract: AcceptanceContract | Mapping[str, Any]) -> None:
    value = contract.to_dict() if isinstance(contract, AcceptanceContract) else contract
    verified = verify_acceptance_contract(value)
    atomic_write_json(Path(path), verified.to_dict())


def write_gate_result(path: Path, result: GateResult | Mapping[str, Any]) -> None:
    value = result.to_dict() if isinstance(result, GateResult) else result
    verified = verify_gate_result(value)
    atomic_write_json(Path(path), verified.to_dict())


def write_acceptance_report(path: Path, report: AcceptanceReport | Mapping[str, Any]) -> None:
    value = report.to_dict() if isinstance(report, AcceptanceReport) else report
    verified = verify_acceptance_report(value)
    atomic_write_json(Path(path), verified.to_dict())
