"""Public value types for Operational Acceptance."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .evidence_ledger import EvidenceReference
from .operational_acceptance_common import (
    ACCEPTANCE_SCHEMA_VERSION, CONTRACT_KIND, GATE_RESULT_KIND, REPORT_KIND,
    STATUS_ORDER, AcceptanceValidationError, _GATE_ID_RE, _digest, _exact_fields, _require_text,
)

@dataclass(frozen=True)
class AcceptanceGate:
    gate_id: str
    required: bool = True
    allowed_statuses: tuple[str, ...] = STATUS_ORDER
    required_evidence_roles: tuple[str, ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.gate_id, "gate_id", pattern=_GATE_ID_RE)
        if not isinstance(self.required, bool):
            raise AcceptanceValidationError("gate required must be boolean")
        if any(item not in STATUS_ORDER for item in self.allowed_statuses):
            raise AcceptanceValidationError("allowed_statuses contains unsupported values")
        normalized_statuses = tuple(
            sorted(set(self.allowed_statuses), key=STATUS_ORDER.index)
        )
        if not normalized_statuses:
            raise AcceptanceValidationError("allowed_statuses must not be empty")
        if "pass" not in normalized_statuses:
            raise AcceptanceValidationError("allowed_statuses must include pass")
        object.__setattr__(self, "allowed_statuses", normalized_statuses)
        roles = tuple(sorted(set(self.required_evidence_roles)))
        for role in roles:
            _require_text(role, "required evidence role")
            if len(role) > 128:
                raise AcceptanceValidationError("required evidence role is too long")
        object.__setattr__(self, "required_evidence_roles", roles)
        if self.description is not None:
            description = _require_text(self.description, "gate description")
            if len(description) > 512:
                raise AcceptanceValidationError("gate description is too long")

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_statuses": list(self.allowed_statuses),
            "description": self.description,
            "gate_id": self.gate_id,
            "required": self.required,
            "required_evidence_roles": list(self.required_evidence_roles),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AcceptanceGate":
        _exact_fields(
            value,
            {
                "gate_id",
                "required",
                "allowed_statuses",
                "required_evidence_roles",
                "description",
            },
            "acceptance gate",
        )
        if not isinstance(value["allowed_statuses"], list):
            raise AcceptanceValidationError("allowed_statuses must be an array")
        if not isinstance(value["required_evidence_roles"], list):
            raise AcceptanceValidationError("required_evidence_roles must be an array")
        return cls(
            gate_id=value["gate_id"],
            required=value["required"],
            allowed_statuses=tuple(value["allowed_statuses"]),
            required_evidence_roles=tuple(value["required_evidence_roles"]),
            description=value["description"],
        )


@dataclass(frozen=True)
class AcceptanceContract:
    schema_version: int
    kind: str
    subject_id: str
    gates: tuple[AcceptanceGate, ...]
    verdict_policy: Mapping[str, str]
    contract_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "gates", tuple(self.gates))
        object.__setattr__(
            self,
            "verdict_policy",
            MappingProxyType(dict(self.verdict_policy)),
        )
        unsigned = {
            "gates": [gate.to_dict() for gate in self.gates],
            "kind": self.kind,
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "verdict_policy": dict(self.verdict_policy),
        }
        if self.contract_sha256 != _digest(unsigned):
            raise AcceptanceValidationError("acceptance contract digest mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_sha256": self.contract_sha256,
            "gates": [gate.to_dict() for gate in self.gates],
            "kind": self.kind,
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "verdict_policy": dict(self.verdict_policy),
        }


@dataclass(frozen=True)
class GateResult:
    schema_version: int
    kind: str
    contract_sha256: str
    subject_id: str
    gate_id: str
    status: str
    reason: str
    evidence: tuple[EvidenceReference, ...]
    result_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_sha256": self.contract_sha256,
            "evidence": [item.to_dict() for item in self.evidence],
            "gate_id": self.gate_id,
            "kind": self.kind,
            "reason": self.reason,
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "status": self.status,
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True)
class AcceptanceReport:
    schema_version: int
    kind: str
    contract_sha256: str
    subject_id: str
    conformance_status: str
    task_verdict: str
    ordered_results: tuple[GateResult, ...]
    missing_required_gates: tuple[str, ...]
    failed_gates: tuple[str, ...]
    blocked_required_gates: tuple[str, ...]
    skipped_required_gates: tuple[str, ...]
    report_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_required_gates": list(self.blocked_required_gates),
            "conformance_status": self.conformance_status,
            "contract_sha256": self.contract_sha256,
            "failed_gates": list(self.failed_gates),
            "kind": self.kind,
            "missing_required_gates": list(self.missing_required_gates),
            "ordered_results": [item.to_dict() for item in self.ordered_results],
            "report_sha256": self.report_sha256,
            "schema_version": self.schema_version,
            "skipped_required_gates": list(self.skipped_required_gates),
            "subject_id": self.subject_id,
            "task_verdict": self.task_verdict,
        }


