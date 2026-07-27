"""Contract construction and validation for Operational Acceptance."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .operational_acceptance_common import (
    ACCEPTANCE_SCHEMA_VERSION, CONTRACT_KIND, VERDICT_POLICY,
    AcceptanceValidationError, _ID_RE, _contract_unsigned, _digest, _exact_fields,
    _require_sha, _require_text,
)
from .operational_acceptance_types import AcceptanceContract, AcceptanceGate
from .state import load_json_object

def build_acceptance_contract(
    *,
    subject_id: str,
    gates: Sequence[AcceptanceGate | Mapping[str, Any]],
    failed_optional: str = "fail",
) -> AcceptanceContract:
    """Build and verify one deterministic acceptance contract."""
    subject = _require_text(subject_id, "subject_id", pattern=_ID_RE)
    normalized = [
        item if isinstance(item, AcceptanceGate) else AcceptanceGate.from_mapping(item)
        for item in gates
    ]
    if not normalized:
        raise AcceptanceValidationError("acceptance contract must define at least one gate")
    normalized.sort(key=lambda item: item.gate_id)
    ids = [item.gate_id for item in normalized]
    if len(set(ids)) != len(ids):
        raise AcceptanceValidationError("acceptance gate ids must be unique")
    if failed_optional not in {"fail", "ignore"}:
        raise AcceptanceValidationError("failed_optional must be fail or ignore")
    policy = dict(VERDICT_POLICY)
    policy["failed_optional"] = failed_optional
    unsigned = {
        "gates": [item.to_dict() for item in normalized],
        "kind": CONTRACT_KIND,
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "subject_id": subject,
        "verdict_policy": policy,
    }
    value = dict(unsigned)
    value["contract_sha256"] = _digest(unsigned)
    return verify_acceptance_contract(value)


def verify_acceptance_contract(value: Mapping[str, Any]) -> AcceptanceContract:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "subject_id",
            "gates",
            "verdict_policy",
            "contract_sha256",
        },
        "acceptance contract",
    )
    if value["schema_version"] != ACCEPTANCE_SCHEMA_VERSION or value["kind"] != CONTRACT_KIND:
        raise AcceptanceValidationError("unsupported acceptance contract schema")
    subject = _require_text(value["subject_id"], "subject_id", pattern=_ID_RE)
    if not isinstance(value["gates"], list) or not value["gates"]:
        raise AcceptanceValidationError("contract gates must be a non-empty array")
    gates = tuple(
        AcceptanceGate.from_mapping(item)
        for item in value["gates"]
        if isinstance(item, dict)
    )
    if len(gates) != len(value["gates"]):
        raise AcceptanceValidationError("contract gates must be JSON objects")
    if [gate.to_dict() for gate in gates] != value["gates"]:
        raise AcceptanceValidationError("contract gates are not canonically normalized")
    if tuple(gate.gate_id for gate in gates) != tuple(sorted(gate.gate_id for gate in gates)):
        raise AcceptanceValidationError("contract gates must be sorted by gate_id")
    if len({gate.gate_id for gate in gates}) != len(gates):
        raise AcceptanceValidationError("contract gate ids must be unique")
    policy = value["verdict_policy"]
    if not isinstance(policy, dict):
        raise AcceptanceValidationError("verdict_policy must be a JSON object")
    _exact_fields(policy, set(VERDICT_POLICY), "verdict_policy")
    expected_policy = dict(VERDICT_POLICY)
    expected_policy["failed_optional"] = policy["failed_optional"]
    if policy["failed_optional"] not in {"fail", "ignore"} or policy != expected_policy:
        raise AcceptanceValidationError("unsupported verdict_policy")
    contract_sha = _require_sha(value["contract_sha256"], "contract_sha256")
    if contract_sha != _digest(_contract_unsigned(value)):
        raise AcceptanceValidationError("acceptance contract digest mismatch")
    return AcceptanceContract(
        schema_version=ACCEPTANCE_SCHEMA_VERSION,
        kind=CONTRACT_KIND,
        subject_id=subject,
        gates=gates,
        verdict_policy=dict(policy),
        contract_sha256=contract_sha,
    )


def load_acceptance_contract(path: Path) -> AcceptanceContract:
    value = load_json_object(Path(path))
    if value is None:
        raise AcceptanceValidationError("acceptance contract is missing or invalid")
    return verify_acceptance_contract(value)
