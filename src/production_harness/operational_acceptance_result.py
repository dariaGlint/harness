"""Gate result construction and validation for Operational Acceptance."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_ledger import EvidenceReference, LedgerValidationError
from .operational_acceptance_common import (
    ACCEPTANCE_SCHEMA_VERSION, GATE_RESULT_KIND, STATUS_ORDER,
    AcceptanceValidationError, _GATE_ID_RE, _ID_RE, _digest, _exact_fields,
    _require_sha, _require_text, _result_unsigned,
)
from .operational_acceptance_contract import verify_acceptance_contract
from .operational_acceptance_types import AcceptanceContract, GateResult
from .state import load_json_object

def build_gate_result(
    contract: AcceptanceContract | Mapping[str, Any],
    *,
    gate_id: str,
    status: str,
    reason: str,
    evidence: Sequence[EvidenceReference | Mapping[str, Any]] = (),
) -> GateResult:
    verified = (
        contract
        if isinstance(contract, AcceptanceContract)
        else verify_acceptance_contract(contract)
    )
    references: list[EvidenceReference] = []
    for item in evidence:
        if isinstance(item, EvidenceReference):
            references.append(item)
            continue
        try:
            references.append(EvidenceReference.from_mapping(item))
        except LedgerValidationError as exc:
            raise AcceptanceValidationError(str(exc)) from exc
    references.sort(key=lambda item: (item.role, item.path))
    result_value = {
        "contract_sha256": verified.contract_sha256,
        "evidence": [item.to_dict() for item in references],
        "gate_id": gate_id,
        "kind": GATE_RESULT_KIND,
        "reason": reason,
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "status": status,
        "subject_id": verified.subject_id,
    }
    result_value["result_sha256"] = _digest(result_value)
    return verify_gate_result(result_value, contract=verified)


def verify_gate_result(
    value: Mapping[str, Any],
    *,
    contract: AcceptanceContract | Mapping[str, Any] | None = None,
) -> GateResult:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "contract_sha256",
            "subject_id",
            "gate_id",
            "status",
            "reason",
            "evidence",
            "result_sha256",
        },
        "gate result",
    )
    if value["schema_version"] != ACCEPTANCE_SCHEMA_VERSION or value["kind"] != GATE_RESULT_KIND:
        raise AcceptanceValidationError("unsupported gate result schema")
    contract_sha = _require_sha(value["contract_sha256"], "contract_sha256")
    subject = _require_text(value["subject_id"], "subject_id", pattern=_ID_RE)
    gate_id = _require_text(value["gate_id"], "gate_id", pattern=_GATE_ID_RE)
    status = _require_text(value["status"], "status")
    if status not in STATUS_ORDER:
        raise AcceptanceValidationError("unsupported gate result status")
    reason = _require_text(value["reason"], "reason")
    if len(reason) > 2048:
        raise AcceptanceValidationError("gate result reason is too long")
    evidence_value = value["evidence"]
    if not isinstance(evidence_value, list):
        raise AcceptanceValidationError("gate result evidence must be an array")
    refs: list[EvidenceReference] = []
    for item in evidence_value:
        if not isinstance(item, dict):
            raise AcceptanceValidationError("gate result evidence entries must be objects")
        try:
            refs.append(EvidenceReference.from_mapping(item))
        except LedgerValidationError as exc:
            raise AcceptanceValidationError(str(exc)) from exc
    refs.sort(key=lambda item: (item.role, item.path))
    if [item.to_dict() for item in refs] != evidence_value:
        raise AcceptanceValidationError("gate result evidence must be sorted by role and path")
    if len({(item.role, item.path) for item in refs}) != len(refs):
        raise AcceptanceValidationError("duplicate gate result evidence references")
    result_sha = _require_sha(value["result_sha256"], "result_sha256")
    if result_sha != _digest(_result_unsigned(value)):
        raise AcceptanceValidationError("gate result digest mismatch")
    result = GateResult(
        schema_version=ACCEPTANCE_SCHEMA_VERSION,
        kind=GATE_RESULT_KIND,
        contract_sha256=contract_sha,
        subject_id=subject,
        gate_id=gate_id,
        status=status,
        reason=reason,
        evidence=tuple(refs),
        result_sha256=result_sha,
    )
    if contract is not None:
        verified_contract = (
            contract
            if isinstance(contract, AcceptanceContract)
            else verify_acceptance_contract(contract)
        )
        if result.contract_sha256 != verified_contract.contract_sha256:
            raise AcceptanceValidationError("gate result contract digest mismatch")
        if result.subject_id != verified_contract.subject_id:
            raise AcceptanceValidationError("gate result subject mismatch")
        gates = {item.gate_id: item for item in verified_contract.gates}
        if result.gate_id not in gates:
            raise AcceptanceValidationError(f"unexpected gate result: {result.gate_id}")
        if result.status not in gates[result.gate_id].allowed_statuses:
            raise AcceptanceValidationError(
                f"gate {result.gate_id} does not allow status {result.status}"
            )
    return result


def load_gate_result(
    path: Path,
    *,
    contract: AcceptanceContract | Mapping[str, Any] | None = None,
) -> GateResult:
    value = load_json_object(Path(path))
    if value is None:
        raise AcceptanceValidationError(f"gate result is missing or invalid: {path}")
    return verify_gate_result(value, contract=contract)
