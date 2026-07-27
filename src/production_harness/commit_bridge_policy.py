"""Repository policy loading and path admission for Workspace Commit Bridge."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .commit_bridge_types import (
    DEFAULT_ALLOWED_BRANCH_PREFIXES,
    DEFAULT_FORBIDDEN_BASENAMES,
    DEFAULT_FORBIDDEN_PATTERNS,
    DEFAULT_FORBIDDEN_PREFIXES,
    DEFAULT_FORBIDDEN_SUFFIXES,
    DEFAULT_IMAGE_SUFFIXES,
    DEFAULT_REQUIRED_HANDOFF_FIELDS,
    DEFAULT_UTF8_TEXT_PATTERNS,
    DEFAULT_VERIFICATION_ARTIFACT_PATTERNS,
    POLICY_SCHEMA_VERSION,
    AdmissionPolicy,
    BridgeLimits,
    CommitBridgePolicy,
    FileInput,
    PolicyValidationError,
    _REPOSITORY_RE,
)

_POLICY_FIELDS = frozenset(
    {
        "$schema",
        "schema_version",
        "repository",
        "allowed_branch_prefixes",
        "forbid_default_branch_update",
        "require_one_tree_one_commit",
        "require_compare",
        "require_latest_default_branch",
        "reject_unexpected_archive_entries",
        "reject_empty_files_unless_allowed",
        "draft_pr_default",
        "fail_closed_on_unexpected_files",
        "forbidden_prefixes",
        "forbidden_suffixes",
        "forbidden_basenames",
        "forbidden_patterns",
        "verification_artifact_patterns",
        "allowed_asset_patterns",
        "image_suffixes",
        "utf8_text_patterns",
        "required_handoff_fields",
        "player_experience_requires_preview",
        "admission",
    }
)
_ADMISSION_FIELDS = frozenset(
    {
        "search_path",
        "issue_claim_factory",
        "issue_claim_verify_method",
        "issue_claim_bind_method",
        "commit_message_callable",
        "required_handoff_fields",
    }
)


def _read_limited_bytes(source: FileInput, limit: int, label: str) -> tuple[bytes, Path | None]:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source).resolve()
        try:
            size = path.stat().st_size
            if size > limit:
                raise PolicyValidationError("policy_too_large", f"{label} exceeds {limit} bytes")
            return path.read_bytes(), path
        except OSError as exc:
            raise PolicyValidationError("policy_unreadable", f"cannot read {label}: {exc}") from exc
    try:
        raw = source.read(limit + 1)
    except (AttributeError, OSError) as exc:
        raise PolicyValidationError("policy_unreadable", f"cannot read {label}: {exc}") from exc
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        data = raw
    else:
        raise PolicyValidationError("policy_unreadable", f"{label} stream must return str or bytes")
    if len(data) > limit:
        raise PolicyValidationError("policy_too_large", f"{label} exceeds {limit} bytes")
    return data, None


def _string_list(raw: Any, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise PolicyValidationError("invalid_policy_field", f"{label} must be a string array")
    if not allow_empty and not raw:
        raise PolicyValidationError("invalid_policy_field", f"{label} must not be empty")
    return tuple(dict.fromkeys(raw))


def _mandatory_true(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key, True)
    if value is not True:
        raise PolicyValidationError("unsafe_policy_relaxation", f"{key} must be true")
    return True


def _union(defaults: Iterable[str], supplied: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*defaults, *supplied]))


def _admission_policy(raw: Any) -> AdmissionPolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PolicyValidationError("invalid_admission_policy", "admission must be an object")
    unknown = sorted(set(raw) - _ADMISSION_FIELDS)
    if unknown:
        raise PolicyValidationError(
            "invalid_admission_policy", f"admission contains unsupported fields: {unknown}"
        )
    search_path = raw.get("search_path", ".")
    if not isinstance(search_path, str) or not search_path or Path(search_path).is_absolute():
        raise PolicyValidationError(
            "invalid_admission_policy", "admission.search_path must be repository-relative"
        )
    for part in Path(search_path).parts:
        if part in {"", ".", ".."} and search_path != ".":
            raise PolicyValidationError(
                "invalid_admission_policy", "admission.search_path contains unsafe components"
            )
    values: dict[str, str | None] = {}
    for key in ("issue_claim_factory", "commit_message_callable"):
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, str)
            or value.count(":") != 1
            or not all(part.strip() for part in value.split(":"))
        ):
            raise PolicyValidationError(
                "invalid_admission_policy", f"admission.{key} must use module:callable form"
            )
        values[key] = value
    for key, default in (
        ("issue_claim_verify_method", "verify"),
        ("issue_claim_bind_method", "bind_head"),
    ):
        value = raw.get(key, default)
        if not isinstance(value, str) or not value.isidentifier():
            raise PolicyValidationError(
                "invalid_admission_policy", f"admission.{key} must be a Python identifier"
            )
        values[key] = value
    required = _string_list(raw.get("required_handoff_fields", []), "admission.required_handoff_fields")
    return AdmissionPolicy(
        search_path=search_path,
        issue_claim_factory=values["issue_claim_factory"],
        issue_claim_verify_method=str(values["issue_claim_verify_method"]),
        issue_claim_bind_method=str(values["issue_claim_bind_method"]),
        commit_message_callable=values["commit_message_callable"],
        required_handoff_fields=required,
    )


def load_commit_bridge_policy(
    policy_json: FileInput | None,
    *,
    limits: BridgeLimits = BridgeLimits(),
) -> CommitBridgePolicy:
    """Load a repository policy without permitting mandatory safety controls to be disabled."""
    if policy_json is None:
        return CommitBridgePolicy()
    data, source_path = _read_limited_bytes(policy_json, limits.max_policy_bytes, "policy_json")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyValidationError("invalid_policy_json", f"invalid policy_json: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise PolicyValidationError("invalid_policy_json", "policy_json root must be an object")
    unknown = sorted(set(payload) - _POLICY_FIELDS)
    if unknown:
        raise PolicyValidationError("invalid_policy_field", f"unsupported policy fields: {unknown}")
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise PolicyValidationError(
            "unsupported_policy_schema",
            f"unsupported policy schema_version: {payload.get('schema_version')!r}",
        )
    repository = payload.get("repository")
    if repository is not None and (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or repository.endswith(".git")
    ):
        raise PolicyValidationError("invalid_policy_repository", f"invalid repository: {repository!r}")
    branch_prefixes = _string_list(
        payload.get("allowed_branch_prefixes", list(DEFAULT_ALLOWED_BRANCH_PREFIXES)),
        "allowed_branch_prefixes",
        allow_empty=False,
    )
    for prefix in branch_prefixes:
        if prefix in {"main", "master"} or not prefix.endswith("/"):
            raise PolicyValidationError(
                "invalid_branch_prefix", f"branch prefix must end with '/': {prefix!r}"
            )
    forbidden_prefixes = _union(
        DEFAULT_FORBIDDEN_PREFIXES,
        _string_list(payload.get("forbidden_prefixes", []), "forbidden_prefixes"),
    )
    forbidden_suffixes = _union(
        DEFAULT_FORBIDDEN_SUFFIXES,
        tuple(item.lower() for item in _string_list(payload.get("forbidden_suffixes", []), "forbidden_suffixes")),
    )
    forbidden_basenames = frozenset(
        _union(
            DEFAULT_FORBIDDEN_BASENAMES,
            _string_list(payload.get("forbidden_basenames", []), "forbidden_basenames"),
        )
    )
    forbidden_patterns = _union(
        DEFAULT_FORBIDDEN_PATTERNS,
        _string_list(payload.get("forbidden_patterns", []), "forbidden_patterns"),
    )
    verification_patterns = _union(
        DEFAULT_VERIFICATION_ARTIFACT_PATTERNS,
        _string_list(
            payload.get("verification_artifact_patterns", []),
            "verification_artifact_patterns",
        ),
    )
    image_suffixes = tuple(
        dict.fromkeys(
            [
                *DEFAULT_IMAGE_SUFFIXES,
                *(item.lower() for item in _string_list(payload.get("image_suffixes", []), "image_suffixes")),
            ]
        )
    )
    required_handoff = _union(
        DEFAULT_REQUIRED_HANDOFF_FIELDS,
        _string_list(payload.get("required_handoff_fields", []), "required_handoff_fields"),
    )
    return CommitBridgePolicy(
        repository=repository,
        allowed_branch_prefixes=branch_prefixes,
        forbid_default_branch_update=_mandatory_true(payload, "forbid_default_branch_update"),
        require_one_tree_one_commit=_mandatory_true(payload, "require_one_tree_one_commit"),
        require_compare=_mandatory_true(payload, "require_compare"),
        require_latest_default_branch=_mandatory_true(payload, "require_latest_default_branch"),
        reject_unexpected_archive_entries=_mandatory_true(payload, "reject_unexpected_archive_entries"),
        reject_empty_files_unless_allowed=_mandatory_true(
            payload, "reject_empty_files_unless_allowed"
        ),
        draft_pr_default=_mandatory_true(payload, "draft_pr_default"),
        fail_closed_on_unexpected_files=_mandatory_true(
            payload, "fail_closed_on_unexpected_files"
        ),
        forbidden_prefixes=forbidden_prefixes,
        forbidden_suffixes=forbidden_suffixes,
        forbidden_basenames=forbidden_basenames,
        forbidden_patterns=forbidden_patterns,
        verification_artifact_patterns=verification_patterns,
        allowed_asset_patterns=_string_list(
            payload.get("allowed_asset_patterns", []), "allowed_asset_patterns"
        ),
        image_suffixes=image_suffixes,
        utf8_text_patterns=_union(
            DEFAULT_UTF8_TEXT_PATTERNS,
            _string_list(payload.get("utf8_text_patterns", []), "utf8_text_patterns"),
        ),
        required_handoff_fields=required_handoff,
        player_experience_requires_preview=_mandatory_true(
            payload, "player_experience_requires_preview"
        ),
        admission=_admission_policy(payload.get("admission")),
        source_path=source_path,
    )


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Match repository paths, including root files against ``**/`` patterns."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
            return True
    return False


def policy_path_rejection(path: str, policy: CommitBridgePolicy) -> tuple[str, str] | None:
    """Return a stable rejection code/message for a forbidden repository path."""
    lowered = path.lower()
    basename = path.rsplit("/", 1)[-1]
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in policy.forbidden_prefixes):
        return "forbidden_path", f"forbidden prefix: {path}"
    if lowered.endswith(tuple(policy.forbidden_suffixes)):
        return "forbidden_artifact", f"forbidden suffix: {path}"
    if basename in policy.forbidden_basenames:
        return "forbidden_artifact", f"forbidden basename: {path}"
    if matches_any(path, policy.forbidden_patterns):
        return "forbidden_artifact", f"forbidden pattern: {path}"
    suffix = Path(path).suffix.lower()
    if suffix in policy.image_suffixes:
        if matches_any(path, policy.verification_artifact_patterns):
            return "verification_artifact", f"verification image is not publishable: {path}"
        if not policy.allowed_asset_patterns or not matches_any(path, policy.allowed_asset_patterns):
            return "unapproved_image_asset", f"image is outside allowed asset paths: {path}"
    return None


def policy_requires_utf8(path: str, policy: CommitBridgePolicy) -> bool:
    return matches_any(path, policy.utf8_text_patterns)
