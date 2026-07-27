"""Handoff manifest parsing and repository-path validation."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .commit_bridge_policy import policy_requires_utf8
from .commit_bridge_types import (
    HANDOFF_SCHEMA_VERSION,
    VALID_ENCODINGS,
    VALID_FILE_MODES,
    VALID_OPERATIONS,
    BridgeLimits,
    CommitBridgePolicy,
    FileInput,
    HandoffFile,
    HandoffManifest,
    HandoffValidationError,
    _REPOSITORY_RE,
    _SHA256_RE,
    _SHA_RE,
)

_MACHINE_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "base_sha",
        "workspace_root",
        "files",
        "direct_dependencies",
        "issue_number",
    }
)
_FILE_FIELDS = frozenset(
    {
        "path",
        "operation",
        "mode",
        "size_bytes",
        "sha256",
        "git_blob_sha",
        "purpose",
        "encoding",
        "allow_empty",
    }
)


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.lower()):
        raise HandoffValidationError("invalid_git_sha", f"invalid {label}: {value!r}")
    return value.lower()


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.lower()):
        raise HandoffValidationError("invalid_sha256", f"invalid {label}: {value!r}")
    return value.lower()


def _validate_repository(repository: object) -> str:
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or repository.endswith(".git")
    ):
        raise HandoffValidationError("invalid_repository", f"invalid repository: {repository!r}")
    return repository


def validate_branch_name(branch: str) -> str:
    value = branch.strip()
    components = value.split("/")
    invalid = (
        not value
        or value == "@"
        or value.startswith("/")
        or value.endswith("/")
        or any(
            not component
            or component.startswith(".")
            or component.endswith(".")
            or component.endswith(".lock")
            for component in components
        )
        or ".." in value
        or "//" in value
        or "@{" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in " ~^:?*[" for character in value)
    )
    if invalid:
        raise HandoffValidationError("invalid_branch_name", f"invalid branch name: {branch!r}")
    return value


def validate_commit_message(message: str) -> str:
    if not isinstance(message, str):
        raise HandoffValidationError("invalid_commit_message", "commit_message must be a string")
    value = message.strip()
    if not value or "\0" in value:
        raise HandoffValidationError(
            "invalid_commit_message", "commit_message must be non-empty and contain no NUL"
        )
    if len(value.encode("utf-8")) > 65_536:
        raise HandoffValidationError(
            "invalid_commit_message", "commit_message exceeds 65536 UTF-8 bytes"
        )
    return value


def normalize_repo_path(raw: object, label: str = "path") -> str:
    if not isinstance(raw, str) or not raw:
        raise HandoffValidationError("invalid_path", f"{label} must be a non-empty string")
    if "\0" in raw or "\\" in raw:
        raise HandoffValidationError("invalid_path", f"invalid {label}: {raw!r}")
    normalized_unicode = unicodedata.normalize("NFC", raw)
    if normalized_unicode != raw:
        raise HandoffValidationError(
            "non_normalized_path", f"{label} must already be Unicode NFC: {raw!r}"
        )
    raw_parts = raw.split("/")
    path = PurePosixPath(raw)
    value = path.as_posix()
    if (
        raw.startswith("/")
        or raw.endswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or value in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise HandoffValidationError("invalid_path", f"invalid {label}: {raw!r}")
    return value


def _normalize_workspace_root(raw: object) -> str:
    if raw in (None, ""):
        return ""
    return normalize_repo_path(raw, "workspace_root").rstrip("/")


def _read_limited_bytes(source: FileInput, limit: int, label: str) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            size = path.stat().st_size
            if size > limit:
                raise HandoffValidationError("handoff_too_large", f"{label} exceeds {limit} bytes")
            data = path.read_bytes()
        except OSError as exc:
            raise HandoffValidationError("handoff_unreadable", f"cannot read {label}: {exc}") from exc
        return data
    try:
        raw = source.read(limit + 1)
    except (AttributeError, OSError) as exc:
        raise HandoffValidationError("handoff_unreadable", f"cannot read {label}: {exc}") from exc
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        data = raw
    else:
        raise HandoffValidationError(
            "handoff_unreadable", f"{label} stream must return str or bytes"
        )
    if len(data) > limit:
        raise HandoffValidationError("handoff_too_large", f"{label} exceeds {limit} bytes")
    return data


def _positive_issue(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HandoffValidationError("invalid_issue_number", "issue_number must be a positive integer")
    return value


def _required_top_level(payload: Mapping[str, Any], policy: CommitBridgePolicy) -> None:
    missing = [key for key in policy.required_handoff_fields if key not in payload]
    if policy.admission is not None:
        missing.extend(
            key for key in policy.admission.required_handoff_fields if key not in payload
        )
    missing = sorted(set(missing))
    if missing:
        raise HandoffValidationError(
            "missing_handoff_fields", f"handoff.json is missing required fields: {missing}"
        )


def _machine_section(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("commit_bridge")
    if nested is None:
        return payload
    if not isinstance(nested, Mapping):
        raise HandoffValidationError("invalid_machine_contract", "commit_bridge must be an object")
    unknown = sorted(set(nested) - _MACHINE_FIELDS)
    if unknown:
        raise HandoffValidationError(
            "unknown_machine_fields", f"commit_bridge contains unsupported fields: {unknown}"
        )
    return nested


def _parse_file(raw: object, policy: CommitBridgePolicy) -> HandoffFile:
    if not isinstance(raw, Mapping):
        raise HandoffValidationError("invalid_file_entry", "each files entry must be an object")
    unknown = sorted(set(raw) - _FILE_FIELDS)
    if unknown:
        raise HandoffValidationError(
            "unknown_file_fields", f"file entry contains unsupported fields: {unknown}"
        )
    path = normalize_repo_path(raw.get("path"))
    operation = raw.get("operation")
    if operation not in VALID_OPERATIONS:
        raise HandoffValidationError(
            "invalid_operation",
            f"unsupported operation for {path}: {operation!r}; expected add, modify, or delete",
        )
    purpose = raw.get("purpose")
    if purpose is not None and (not isinstance(purpose, str) or not purpose.strip()):
        raise HandoffValidationError("invalid_purpose", f"purpose must be non-empty for {path}")
    if operation == "delete":
        unsupported = sorted(set(raw) - {"path", "operation", "purpose"})
        if unsupported:
            raise HandoffValidationError(
                "invalid_delete_entry", f"delete entry {path} has unsupported fields: {unsupported}"
            )
        return HandoffFile(path=path, operation=operation, purpose=purpose.strip() if purpose else None)
    mode = raw.get("mode")
    if mode not in VALID_FILE_MODES:
        raise HandoffValidationError("invalid_mode", f"unsupported mode for {path}: {mode!r}")
    size_bytes = raw.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise HandoffValidationError(
            "invalid_size", f"size_bytes must be a non-negative integer for {path}"
        )
    allow_empty = raw.get("allow_empty", False)
    if not isinstance(allow_empty, bool):
        raise HandoffValidationError("invalid_allow_empty", f"allow_empty must be boolean for {path}")
    if size_bytes == 0 and policy.reject_empty_files_unless_allowed and not allow_empty:
        raise HandoffValidationError(
            "unexpected_empty_file", f"empty file requires allow_empty=true: {path}"
        )
    encoding = raw.get("encoding")
    if encoding is None:
        encoding = "utf-8" if policy_requires_utf8(path, policy) else "binary"
    if encoding not in VALID_ENCODINGS:
        raise HandoffValidationError(
            "invalid_encoding", f"encoding must be utf-8 or binary for {path}"
        )
    return HandoffFile(
        path=path,
        operation=operation,
        mode=mode,
        size_bytes=size_bytes,
        sha256=_validate_sha256(raw.get("sha256"), f"sha256 for {path}"),
        git_blob_sha=_validate_sha(raw.get("git_blob_sha"), f"git_blob_sha for {path}"),
        purpose=purpose.strip() if purpose else None,
        encoding=encoding,
        allow_empty=allow_empty,
    )


def _changed_file_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any] | None]:
    raw = payload.get("changed_files")
    if not isinstance(raw, list) or not raw:
        raise HandoffValidationError("invalid_changed_files", "changed_files must be a non-empty array")
    result: dict[str, Mapping[str, Any] | None] = {}
    folded: dict[str, str] = {}
    for index, item in enumerate(raw):
        if isinstance(item, str):
            path = normalize_repo_path(item, f"changed_files[{index}]")
            details = None
        elif isinstance(item, Mapping):
            path = normalize_repo_path(item.get("path"), f"changed_files[{index}].path")
            details = item
        else:
            raise HandoffValidationError(
                "invalid_changed_files", "changed_files entries must be path strings or objects"
            )
        if path in result:
            raise HandoffValidationError("duplicate_path", f"duplicate changed_files path: {path}")
        previous = folded.get(path.casefold())
        if previous is not None:
            raise HandoffValidationError(
                "case_collision", f"case-colliding changed_files paths: {previous!r}, {path!r}"
            )
        folded[path.casefold()] = path
        result[path] = details
    return result


def _reconcile_changed_files(
    payload: Mapping[str, Any], files: tuple[HandoffFile, ...]
) -> tuple[HandoffFile, ...]:
    declared = _changed_file_map(payload)
    machine = {item.path: item for item in files}
    if set(declared) != set(machine):
        missing = sorted(set(machine) - set(declared))
        unexpected = sorted(set(declared) - set(machine))
        raise HandoffValidationError(
            "changed_files_mismatch",
            f"changed_files and commit_bridge.files differ: missing={missing}, unexpected={unexpected}",
        )
    merged: list[HandoffFile] = []
    for path in sorted(machine):
        item = machine[path]
        details = declared[path]
        purpose = item.purpose
        if details is not None:
            if "sha256" in details:
                declared_sha = _validate_sha256(details.get("sha256"), f"changed_files sha256 for {path}")
                if item.sha256 is not None and declared_sha != item.sha256:
                    raise HandoffValidationError(
                        "sha256_contract_mismatch",
                        f"changed_files and machine sha256 differ for {path}",
                    )
            if "purpose" in details:
                raw_purpose = details.get("purpose")
                if not isinstance(raw_purpose, str) or not raw_purpose.strip():
                    raise HandoffValidationError(
                        "invalid_purpose", f"changed_files purpose must be non-empty for {path}"
                    )
                if purpose is not None and purpose != raw_purpose.strip():
                    raise HandoffValidationError(
                        "purpose_contract_mismatch", f"purpose differs for {path}"
                    )
                purpose = raw_purpose.strip()
        merged.append(
            HandoffFile(
                path=item.path,
                operation=item.operation,
                mode=item.mode,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                git_blob_sha=item.git_blob_sha,
                purpose=purpose,
                encoding=item.encoding,
                allow_empty=item.allow_empty,
            )
        )
    return tuple(merged)


def load_handoff_manifest(
    handoff_json: FileInput,
    *,
    policy: CommitBridgePolicy = CommitBridgePolicy(),
    limits: BridgeLimits = BridgeLimits(),
) -> HandoffManifest:
    """Load a backward-compatible handoff plus the strict machine publication section."""
    data = _read_limited_bytes(handoff_json, limits.max_handoff_bytes, "handoff_json")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffValidationError("invalid_handoff_json", f"invalid handoff_json: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise HandoffValidationError("invalid_handoff_json", "handoff_json root must be an object")
    _required_top_level(payload, policy)
    section = _machine_section(payload)
    if section.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise HandoffValidationError(
            "unsupported_handoff_schema",
            f"unsupported commit_bridge schema_version: {section.get('schema_version')!r}",
        )
    repository = _validate_repository(section.get("repository"))
    top_repository = payload.get("repository")
    if top_repository is not None and _validate_repository(top_repository) != repository:
        raise HandoffValidationError("repository_mismatch", "top-level and machine repository differ")
    base_sha = _validate_sha(section.get("base_sha"), "base_sha")
    top_base = payload.get("base_master_sha")
    if top_base is not None and _validate_sha(top_base, "base_master_sha") != base_sha:
        raise HandoffValidationError("base_sha_mismatch", "base_master_sha and machine base_sha differ")
    files_value = section.get("files")
    if not isinstance(files_value, list) or not files_value:
        raise HandoffValidationError("invalid_files", "files must be a non-empty array")
    if len(files_value) > limits.max_changes:
        raise HandoffValidationError(
            "too_many_files", f"files exceeds max_changes={limits.max_changes}"
        )
    files = tuple(sorted((_parse_file(item, policy) for item in files_value), key=lambda item: item.path))
    seen: set[str] = set()
    folded: dict[str, str] = {}
    for item in files:
        if item.path in seen:
            raise HandoffValidationError("duplicate_path", f"duplicate handoff path: {item.path}")
        seen.add(item.path)
        previous = folded.get(item.path.casefold())
        if previous is not None:
            raise HandoffValidationError(
                "case_collision", f"case-colliding handoff paths: {previous!r}, {item.path!r}"
            )
        folded[item.path.casefold()] = item.path
    files = _reconcile_changed_files(payload, files)
    dependencies_raw = section.get("direct_dependencies", [])
    if not isinstance(dependencies_raw, list) or not all(
        isinstance(item, str) for item in dependencies_raw
    ):
        raise HandoffValidationError(
            "invalid_direct_dependencies", "direct_dependencies must be a string array"
        )
    dependencies = tuple(sorted({normalize_repo_path(item, "direct_dependencies") for item in dependencies_raw}))
    issue_top = payload.get("issue_number")
    issue_nested = section.get("issue_number")
    issue_number: int | None = None
    if issue_top is not None:
        issue_number = _positive_issue(issue_top)
    if issue_nested is not None:
        nested_value = _positive_issue(issue_nested)
        if issue_number is not None and nested_value != issue_number:
            raise HandoffValidationError("issue_number_mismatch", "issue_number values differ")
        issue_number = nested_value
    behavior_change = payload.get("behavior_change", False)
    if not isinstance(behavior_change, bool):
        raise HandoffValidationError("invalid_behavior_change", "behavior_change must be boolean")
    preview_approved = payload.get("preview_approved")
    if preview_approved is not None and not isinstance(preview_approved, bool):
        raise HandoffValidationError("invalid_preview_approved", "preview_approved must be boolean")
    validation_results = payload.get("validation_results", [])
    if not isinstance(validation_results, list):
        raise HandoffValidationError("invalid_validation_results", "validation_results must be an array")
    next_action = payload.get("next_action", "")
    if not isinstance(next_action, str) or not next_action.strip():
        raise HandoffValidationError("invalid_next_action", "next_action must be non-empty")
    if next_action != "commit":
        raise HandoffValidationError(
            "next_action_not_commit", f"next_action must be 'commit', got {next_action!r}"
        )
    for key in ("task_id", "controller_run_id", "transaction_id"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise HandoffValidationError("invalid_admission_context", f"{key} must be non-empty")
    validation_ownership = payload.get("validation_ownership")
    if validation_ownership is not None and not isinstance(validation_ownership, Mapping):
        raise HandoffValidationError(
            "invalid_validation_ownership", "validation_ownership must be an object"
        )
    return HandoffManifest(
        repository=repository,
        base_sha=base_sha,
        workspace_root=_normalize_workspace_root(section.get("workspace_root", "")),
        files=files,
        direct_dependencies=dependencies,
        issue_number=issue_number,
        behavior_change=behavior_change,
        preview_approved=preview_approved,
        validation_results=tuple(validation_results),
        next_action=next_action,
        task_id=payload.get("task_id"),
        controller_run_id=payload.get("controller_run_id"),
        transaction_id=payload.get("transaction_id"),
        validation_ownership=validation_ownership,
        raw_top_level=dict(payload),
    )
