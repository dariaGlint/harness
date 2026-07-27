"""Deterministic, fail-closed Operational Acceptance contracts."""
from .operational_acceptance_common import (
    ACCEPTANCE_SCHEMA_VERSION,
    CONTRACT_KIND,
    GATE_RESULT_KIND,
    REPORT_KIND,
    STATUS_ORDER,
    AcceptanceError,
    AcceptanceEvidenceError,
    AcceptanceValidationError,
    _contract_unsigned,
    _digest,
    _report_unsigned,
    _result_unsigned,
)
from .operational_acceptance_contract import (
    build_acceptance_contract,
    load_acceptance_contract,
    verify_acceptance_contract,
)
from .operational_acceptance_report import (
    evaluate_acceptance,
    verify_acceptance_report,
    write_acceptance_contract,
    write_acceptance_report,
    write_gate_result,
)
from .operational_acceptance_result import (
    build_gate_result,
    load_gate_result,
    verify_gate_result,
)
from .operational_acceptance_types import (
    AcceptanceContract,
    AcceptanceGate,
    AcceptanceReport,
    GateResult,
)

__all__ = [
    "ACCEPTANCE_SCHEMA_VERSION",
    "CONTRACT_KIND",
    "GATE_RESULT_KIND",
    "REPORT_KIND",
    "STATUS_ORDER",
    "AcceptanceContract",
    "AcceptanceError",
    "AcceptanceEvidenceError",
    "AcceptanceGate",
    "AcceptanceReport",
    "AcceptanceValidationError",
    "GateResult",
    "build_acceptance_contract",
    "build_gate_result",
    "evaluate_acceptance",
    "load_acceptance_contract",
    "load_gate_result",
    "verify_acceptance_contract",
    "verify_acceptance_report",
    "verify_gate_result",
    "write_acceptance_contract",
    "write_acceptance_report",
    "write_gate_result",
]
