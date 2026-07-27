"""Handoff manifest parsing and repository-path validation."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .commit_bridge_types import (
    DEFAULT_FORBIDDEN_BASENAMES, DEFAULT_FORBIDDEN_PREFIXES,
    DEFAULT_FORBIDDEN_SUFFIXES, HANDOFF_SCHEMA_VERSION, VALID_FILE_MODES,
    VALID_OPERATIONS, BridgeLimits, FileInput, HandoffFile, HandoffManifest,
    HandoffValidationError, _REPOSITORY_RE, _SHA_RE,
)

def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.lower()):
        raise HandoffValidationError(f"invalid {label}: {value!r}")
    return value.lower()


def _validate_repository(repository: str) -> str:
    if not _REPOSITORY_RE.fullmatch(repository) or repository.endswith(".git"):
        raise HandoffValidationError(f"invalid repository: {repository!r}")
    return repository


def _validate_branch(branch: str) -> str:
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
        raise HandoffValidationError(f"invalid branch name: {branch!r}")
    return value


def _validate_commit_message(message: str) -> str:
    if not isinstance(message, str):
        raise HandoffValidationError("commit_message must be a string")
    value = message.strip()
    if not value or "\0" in value:
        raise HandoffValidationError("commit_message must be non-empty and contain no NUL")
    if len(value.encode("utf-8")) > 65_536:
        raise HandoffValidationError("commit_message exceeds 65536 UTF-8 bytes")
    return value


def _normalize_repo_path(raw: object, label: str = "path") -> str:
    if not isinstance(raw, str) or not raw:
        raise HandoffValidationError(f"{label} must be a non-empty string")
    if "\0" in raw or "\\" in raw:
        raise HandoffValidationError(f"invalid {label}: {raw!r}")
    normalized_unicode = unicodedata.normalize("NFC", raw)
    if normalized_unicode != raw:
        raise HandoffValidationError(f"{label} must already be Unicode NFC: {raw!r}")
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
        raise HandoffValidationError(f"invalid {label}: {raw!r}")
    return value


def _normalize_workspace_root(raw: object) -> str:
    if raw in (None, ""):
        return ""
    return _normalize_repo_path(raw, "workspace_root").rstrip("/")


def _read_limited_bytes(source: FileInput, limit: int, label: str) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise HandoffValidationError(f"cannot stat {label}: {exc}") from exc
        if size > limit:
            raise HandoffValidationError(f"{label} exceeds {limit} bytes")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise HandoffValidationError(f"cannot read {label}: {exc}") from exc
        if len(data) > limit:
            raise HandoffValidationError(f"{label} exceeds {limit} bytes")
        return data

    try:
        raw = source.read(limit + 1)
    except (AttributeError, OSError) as exc:
        raise HandoffValidationError(f"cannot read {label}: {exc}") from exc
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        data = raw
    else:
        raise HandoffValidationError(f"{label} stream must return str or bytes")
    if len(data) > limit:
        raise HandoffValidationError(f"{label} exceeds {limit} bytes")
    return data


_HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "base_sha",
        "workspace_root",
        "files",
        "direct_dependencies",
        "issue_number",
        "forbidden_prefixes",
        "forbidden_suffixes",
        "forbidden_basenames",
    }
)


def _manifest_section(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("commit_bridge")
    if nested is None:
        unknown = sorted(set(payload) - _HANDOFF_FIELDS)
        if unknown:
            raise HandoffValidationError(f"unknown direct handoff fields: {unknown}")
        return payload
    if not isinstance(nested, Mapping):
        raise HandoffValidationError("commit_bridge must be a JSON object")
    unknown = sorted(set(nested) - _HANDOFF_FIELDS)
    if unknown:
        raise HandoffValidationError(f"unknown commit_bridge fields: {unknown}")
    return nested


def _string_array(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HandoffValidationError(f"{key} must be a string array")
    normalized = tuple(sorted({_normalize_repo_path(item, key) for item in value}))
    return normalized


def _parse_handoff_file(raw: object) -> HandoffFile:
    if not isinstance(raw, Mapping):
        raise HandoffValidationError("each files entry must be an object")
    path = _normalize_repo_path(raw.get("path"))
    operation = raw.get("operation")
    if operation not in VALID_OPERATIONS:
        raise HandoffValidationError(
            f"unsupported operation for {path}: {operation!r}; expected add, modify, or delete"
        )
    if operation == "delete":
        unknown = sorted(set(raw) - {"path", "operation"})
        if unknown:
            raise HandoffValidationError(
                f"delete entry {path} has unsupported fields: {unknown}"
            )
        return HandoffFile(path=path, operation=operation)

    unknown = sorted(
        set(raw) - {"path", "operation", "mode", "size_bytes", "sha256", "git_blob_sha"}
    )
    if unknown:
        raise HandoffValidationError(f"file entry {path} has unsupported fields: {unknown}")
    mode = raw.get("mode")
    if mode not in VALID_FILE_MODES:
        raise HandoffValidationError(f"unsupported mode for {path}: {mode!r}")
    size_bytes = raw.get("size_bytes")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise HandoffValidationError(f"size_bytes must be a non-negative integer for {path}")
    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise HandoffValidationError(f"invalid sha256 for {path}")
    blob_sha = _validate_sha(raw.get("git_blob_sha"), f"git_blob_sha for {path}")
    return HandoffFile(
        path=path,
        operation=operation,
        mode=mode,
        size_bytes=size_bytes,
        sha256=sha256.lower(),
        git_blob_sha=blob_sha,
    )


def load_handoff_manifest(
    handoff_json: FileInput,
    *,
    limits: BridgeLimits = BridgeLimits(),
) -> HandoffManifest:
    """Load and validate the commit section of one ``handoff.json``."""
    data = _read_limited_bytes(handoff_json, limits.max_handoff_bytes, "handoff_json")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffValidationError(f"invalid handoff_json: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise HandoffValidationError("handoff_json root must be an object")
    section = _manifest_section(payload)
    if section.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise HandoffValidationError(
            f"unsupported commit_bridge schema_version: {section.get('schema_version')!r}"
        )
    repository = _validate_repository(str(section.get("repository", "")))
    base_sha = _validate_sha(section.get("base_sha"), "base_sha")
    workspace_root = _normalize_workspace_root(section.get("workspace_root", ""))
    files_value = section.get("files")
    if not isinstance(files_value, list) or not files_value:
        raise HandoffValidationError("files must be a non-empty array")
    if len(files_value) > limits.max_changes:
        raise HandoffValidationError(f"files exceeds max_changes={limits.max_changes}")
    files = tuple(sorted((_parse_handoff_file(item) for item in files_value), key=lambda item: item.path))
    seen: set[str] = set()
    casefolded: dict[str, str] = {}
    for item in files:
        if item.path in seen:
            raise HandoffValidationError(f"duplicate handoff path: {item.path}")
        seen.add(item.path)
        folded = item.path.casefold()
        previous = casefolded.get(folded)
        if previous is not None and previous != item.path:
            raise HandoffValidationError(f"case-colliding handoff paths: {previous!r}, {item.path!r}")
        casefolded[folded] = item.path
    dependencies = _string_array(section, "direct_dependencies")
    root_issue_number = payload.get("issue_number") if section is not payload else None
    section_issue_number = section.get("issue_number")
    if (
        root_issue_number is not None
        and section_issue_number is not None
        and root_issue_number != section_issue_number
    ):
        raise HandoffValidationError("root and commit_bridge issue_number values differ")
    issue_number = root_issue_number if root_issue_number is not None else section_issue_number
    if issue_number is not None and (
        not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0
    ):
        raise HandoffValidationError("issue_number must be a positive integer when present")

    def configured_tuple(key: str, defaults: Sequence[str]) -> tuple[str, ...]:
        value = section.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise HandoffValidationError(f"{key} must be a string array")
        return tuple(dict.fromkeys((*defaults, *value)))

    forbidden_basenames_value = section.get("forbidden_basenames", [])
    if not isinstance(forbidden_basenames_value, list) or not all(
        isinstance(item, str) and item for item in forbidden_basenames_value
    ):
        raise HandoffValidationError("forbidden_basenames must be a string array")
    return HandoffManifest(
        repository=repository,
        base_sha=base_sha,
        workspace_root=workspace_root,
        files=files,
        direct_dependencies=dependencies,
        issue_number=issue_number,
        forbidden_prefixes=configured_tuple("forbidden_prefixes", DEFAULT_FORBIDDEN_PREFIXES),
        forbidden_suffixes=configured_tuple("forbidden_suffixes", DEFAULT_FORBIDDEN_SUFFIXES),
        forbidden_basenames=frozenset((*DEFAULT_FORBIDDEN_BASENAMES, *forbidden_basenames_value)),
    )


def _is_forbidden_path(path: str, manifest: HandoffManifest) -> bool:
    folded = path.casefold()
    return (
        any(
            folded == prefix.rstrip("/").casefold()
            or folded.startswith(prefix.casefold())
            for prefix in manifest.forbidden_prefixes
        )
        or any(folded.endswith(suffix.casefold()) for suffix in manifest.forbidden_suffixes)
        or PurePosixPath(path).name.casefold()
        in {name.casefold() for name in manifest.forbidden_basenames}
    )
