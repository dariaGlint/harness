"""Types and public contracts for Workspace Commit Bridge."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol, Sequence, TextIO, runtime_checkable

HANDOFF_SCHEMA_VERSION = 1
POLICY_SCHEMA_VERSION = 1
VALID_FILE_MODES = frozenset({"100644", "100755"})
VALID_OPERATIONS = frozenset({"add", "modify", "delete"})
VALID_ENCODINGS = frozenset({"utf-8", "binary"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

DEFAULT_ALLOWED_BRANCH_PREFIXES = ("agent/",)
DEFAULT_FORBIDDEN_PREFIXES = (
    ".git/",
    ".godot/",
    "captures/",
    "validation_output/",
    "movie_frames/",
)
DEFAULT_FORBIDDEN_SUFFIXES = (
    ".gif",
    ".mp4",
    ".avi",
    ".mkv",
    ".zip",
    ".log",
)
DEFAULT_FORBIDDEN_BASENAMES = frozenset({"handoff.json", ".handoff.json"})
DEFAULT_FORBIDDEN_PATTERNS = (
    "**/captures/**",
    "**/validation_output/**",
    "**/movie_frames/**",
    "**/*recording*.log",
    "**/*validation*.log",
    "**/Godot_v*.zip",
    "**/godot*.x86_64",
    "**/blender-*.tar.*",
)
DEFAULT_VERIFICATION_ARTIFACT_PATTERNS = (
    "**/*preview*.png",
    "**/*preview*.jpg",
    "**/*preview*.jpeg",
    "**/*comparison*.png",
    "**/*comparison*.jpg",
    "**/*comparison*.jpeg",
    "**/*diff*.png",
    "**/*diff*.jpg",
    "**/*diff*.jpeg",
    "**/*capture*.png",
    "**/*capture*.jpg",
    "**/*capture*.jpeg",
    "**/*screenshot*.png",
    "**/*screenshot*.jpg",
    "**/*screenshot*.jpeg",
)
DEFAULT_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
DEFAULT_UTF8_TEXT_PATTERNS = (
    "**/*.gd",
    "**/*.py",
    "**/*.json",
    "**/*.md",
    "**/*.txt",
    "**/*.toml",
    "**/*.yml",
    "**/*.yaml",
    "**/*.tscn",
    "**/*.tres",
    "**/*.cfg",
    "**/*.ini",
    "**/*.xml",
    "**/*.csv",
    "**/*.shader",
    "**/*.gdshader",
    "**/*.sh",
    "**/*.ps1",
)
DEFAULT_REQUIRED_HANDOFF_FIELDS = (
    "issue_number",
    "repository",
    "base_master_sha",
    "changed_files",
    "behavior_change",
    "validation_results",
    "preview_approved",
    "next_action",
)

FileInput = str | os.PathLike[str] | BinaryIO | TextIO


class CommitBridgeError(RuntimeError):
    """Base class for deterministic fail-closed bridge errors."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class PolicyValidationError(CommitBridgeError):
    """Repository policy is missing, malformed, or unsafe."""


class HandoffValidationError(CommitBridgeError):
    """The handoff manifest is missing, malformed, or inconsistent."""


class CheckpointValidationError(CommitBridgeError):
    """The checkpoint archive is unsafe or does not match the handoff."""


class RepositoryStateError(CommitBridgeError):
    """Remote repository state does not permit a safe publication."""


class AdmissionError(CommitBridgeError):
    """A repository-owned admission hook rejected publication."""


class GitHubPublicationError(CommitBridgeError):
    """GitHub object publication failed or returned inconsistent data."""


@dataclass(frozen=True)
class BridgeLimits:
    """Resource and scope limits applied before any GitHub write."""

    max_handoff_bytes: int = 2 * 1024 * 1024
    max_policy_bytes: int = 1 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_archive_entries: int = 100_000
    max_changes: int = 256
    max_single_file_bytes: int = 100 * 1024 * 1024
    max_selected_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 1_000.0

    def __post_init__(self) -> None:
        integer_fields = (
            "max_handoff_bytes",
            "max_policy_bytes",
            "max_archive_bytes",
            "max_archive_entries",
            "max_changes",
            "max_single_file_bytes",
            "max_selected_bytes",
        )
        for name in integer_fields:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be positive")


@dataclass(frozen=True)
class AdmissionPolicy:
    search_path: str = "."
    issue_claim_factory: str | None = None
    issue_claim_verify_method: str = "verify"
    issue_claim_bind_method: str = "bind_head"
    commit_message_callable: str | None = None
    required_handoff_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitBridgePolicy:
    """Repository-specific restrictions layered over mandatory bridge defaults."""

    repository: str | None = None
    allowed_branch_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_BRANCH_PREFIXES
    forbid_default_branch_update: bool = True
    require_one_tree_one_commit: bool = True
    require_compare: bool = True
    require_latest_default_branch: bool = True
    reject_unexpected_archive_entries: bool = True
    reject_empty_files_unless_allowed: bool = True
    draft_pr_default: bool = True
    fail_closed_on_unexpected_files: bool = True
    forbidden_prefixes: tuple[str, ...] = DEFAULT_FORBIDDEN_PREFIXES
    forbidden_suffixes: tuple[str, ...] = DEFAULT_FORBIDDEN_SUFFIXES
    forbidden_basenames: frozenset[str] = DEFAULT_FORBIDDEN_BASENAMES
    forbidden_patterns: tuple[str, ...] = DEFAULT_FORBIDDEN_PATTERNS
    verification_artifact_patterns: tuple[str, ...] = DEFAULT_VERIFICATION_ARTIFACT_PATTERNS
    allowed_asset_patterns: tuple[str, ...] = ()
    image_suffixes: tuple[str, ...] = DEFAULT_IMAGE_SUFFIXES
    utf8_text_patterns: tuple[str, ...] = DEFAULT_UTF8_TEXT_PATTERNS
    required_handoff_fields: tuple[str, ...] = DEFAULT_REQUIRED_HANDOFF_FIELDS
    player_experience_requires_preview: bool = True
    admission: AdmissionPolicy | None = None
    source_path: Path | None = None


@dataclass(frozen=True)
class HandoffFile:
    """One explicitly approved repository change."""

    path: str
    operation: str
    mode: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    git_blob_sha: str | None = None
    purpose: str | None = None
    encoding: str | None = None
    allow_empty: bool = False


@dataclass(frozen=True)
class HandoffManifest:
    """Validated orchestration and machine publication contract."""

    repository: str
    base_sha: str
    workspace_root: str
    files: tuple[HandoffFile, ...]
    direct_dependencies: tuple[str, ...] = ()
    issue_number: int | None = None
    behavior_change: bool = False
    preview_approved: bool | None = None
    validation_results: tuple[Any, ...] = ()
    next_action: str = ""
    task_id: str | None = None
    controller_run_id: str | None = None
    transaction_id: str | None = None
    validation_ownership: Mapping[str, Any] | None = None
    raw_top_level: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class RepositoryInfo:
    default_branch: str


@dataclass(frozen=True)
class RemotePath:
    path: str
    object_type: str
    sha: str
    mode: str | None = None


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    tree_sha: str
    parent_shas: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class ComparedFile:
    path: str
    status: str


@dataclass(frozen=True)
class CompareResult:
    status: str
    ahead_by: int
    behind_by: int
    files: tuple[ComparedFile, ...]
    files_complete: bool = True


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str | None = None
    draft: bool = True


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    object_type: str = "blob"
    sha: str | None = None


@dataclass(frozen=True)
class PreparedBlob:
    specification: HandoffFile
    data: bytes
    sha256: str
    git_blob_sha: str
    zip_member: str


@dataclass(frozen=True)
class CommitBridgeResult:
    """Machine-readable success or rejection result."""

    status: str
    repository: str
    requested_base_sha: str
    branch: str
    stage: str | None = None
    reason: str | None = None
    message: str | None = None
    effective_base_sha: str | None = None
    base_branch: str | None = None
    tree_sha: str | None = None
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    changed_files: tuple[str, ...] = ()
    compare_verified: bool = False
    tree_created: bool = False
    commit_created: bool = False
    branch_created: bool = False
    branch_updated: bool = False
    pr_created: bool = False
    base_rebased: bool = False
    commit_reused: bool = False
    uploaded_blob_shas: Mapping[str, str] = field(default_factory=dict)
    admission_sha256: str | None = None

    @classmethod
    def rejected(
        cls,
        *,
        repository: str,
        requested_base_sha: str,
        branch: str,
        stage: str,
        reason: str,
        message: str,
        state: Mapping[str, Any] | None = None,
    ) -> "CommitBridgeResult":
        state = dict(state or {})
        return cls(
            status="rejected",
            repository=repository,
            requested_base_sha=requested_base_sha,
            branch=branch,
            stage=stage,
            reason=reason,
            message=message,
            effective_base_sha=state.get("effective_base_sha"),
            base_branch=state.get("base_branch"),
            tree_sha=state.get("tree_sha"),
            commit_sha=state.get("commit_sha"),
            changed_files=tuple(state.get("changed_files", ())),
            compare_verified=bool(state.get("compare_verified", False)),
            tree_created=bool(state.get("tree_created", False)),
            commit_created=bool(state.get("commit_created", False)),
            branch_created=bool(state.get("branch_created", False)),
            branch_updated=bool(state.get("branch_updated", False)),
            pr_created=bool(state.get("pr_created", False)),
            base_rebased=bool(state.get("base_rebased", False)),
            commit_reused=bool(state.get("commit_reused", False)),
            uploaded_blob_shas=dict(state.get("uploaded_blob_shas", {})),
            admission_sha256=state.get("admission_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "reason": self.reason,
            "message": self.message,
            "repository": self.repository,
            "base_sha": self.effective_base_sha or self.requested_base_sha,
            "requested_base_sha": self.requested_base_sha,
            "effective_base_sha": self.effective_base_sha,
            "base_branch": self.base_branch,
            "tree_sha": self.tree_sha,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "changed_files": list(self.changed_files),
            "compare_verified": self.compare_verified,
            "tree_created": self.tree_created,
            "commit_created": self.commit_created,
            "branch_created": self.branch_created,
            "branch_updated": self.branch_updated,
            "pr_created": self.pr_created,
            "base_rebased": self.base_rebased,
            "commit_reused": self.commit_reused,
            "uploaded_blob_shas": dict(self.uploaded_blob_shas),
            "admission_sha256": self.admission_sha256,
        }


@runtime_checkable
class GitHubCommitClient(Protocol):
    """Minimal GitHub object API required by the bridge."""

    def get_repository(self, repository: str) -> RepositoryInfo: ...

    def get_branch_head(self, repository: str, branch: str) -> str | None: ...

    def get_commit(self, repository: str, commit_sha: str) -> CommitInfo: ...

    def get_path(self, repository: str, path: str, ref: str) -> RemotePath | None: ...

    def get_tree_path(self, repository: str, path: str, tree_sha: str) -> RemotePath | None: ...

    def create_blob(self, repository: str, data: bytes) -> str: ...

    def create_tree(
        self,
        repository: str,
        base_tree_sha: str,
        entries: Sequence[TreeEntry],
    ) -> str: ...

    def create_commit(
        self,
        repository: str,
        message: str,
        tree_sha: str,
        parent_sha: str,
    ) -> str: ...

    def compare_commits(self, repository: str, base: str, head: str) -> CompareResult: ...

    def create_branch(self, repository: str, branch: str, commit_sha: str) -> None: ...

    def update_branch(self, repository: str, branch: str, commit_sha: str) -> None: ...

    def find_open_pull_request(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequestInfo | None: ...

    def create_pull_request(
        self,
        repository: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        *,
        draft: bool,
    ) -> PullRequestInfo: ...


def git_blob_sha(data: bytes) -> str:
    """Return the Git SHA-1 object identifier for one blob payload."""
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
