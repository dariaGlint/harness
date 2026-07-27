"""Shared validation primitives for Operational Acceptance."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

ACCEPTANCE_SCHEMA_VERSION = 1
CONTRACT_KIND = "operational_acceptance_contract"
GATE_RESULT_KIND = "operational_acceptance_gate_result"
REPORT_KIND = "operational_acceptance_report"
STATUS_ORDER = ("pass", "fail", "blocked", "skipped")
VERDICT_POLICY = {
    "failed_required": "fail",
    "failed_optional": "fail",
    "missing_required": "blocked",
    "blocked_required": "blocked",
    "skipped_required": "blocked",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_GATE_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AcceptanceError(RuntimeError):
    """Base class for Operational Acceptance failures."""


class AcceptanceValidationError(AcceptanceError):
    """Raised when a contract, result, or report is invalid."""


class AcceptanceEvidenceError(AcceptanceValidationError):
    """Raised when required evidence is missing or changed."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AcceptanceValidationError(f"value is not canonical JSON data: {exc}") from exc


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_text(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AcceptanceValidationError(f"{label} must be a trimmed non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise AcceptanceValidationError(f"{label} has an invalid format")
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AcceptanceValidationError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise AcceptanceValidationError(
            f"{label} fields mismatch; missing={missing}, unexpected={unexpected}"
        )


def _text_tuple(
    values: Any,
    label: str,
    *,
    allowed: set[str] | None = None,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise AcceptanceValidationError(f"{label} must be a JSON array")
    result: list[str] = []
    for item in values:
        text = _require_text(item, label)
        if allowed is not None and text not in allowed:
            raise AcceptanceValidationError(f"{label} contains unsupported value: {text}")
        result.append(text)
    if not allow_empty and not result:
        raise AcceptanceValidationError(f"{label} must not be empty")
    if result != sorted(result):
        raise AcceptanceValidationError(f"{label} must be sorted")
    if len(set(result)) != len(result):
        raise AcceptanceValidationError(f"{label} must not contain duplicates")
    return tuple(result)

def _contract_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("contract_sha256", None)
    return unsigned


def _result_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("result_sha256", None)
    return unsigned


def _report_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("report_sha256", None)
    return unsigned

