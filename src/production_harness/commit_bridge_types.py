"""Types and public contracts for Workspace Commit Bridge."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Mapping, Protocol, Sequence, TextIO, runtime_checkable


HANDOFF_SCHEMA_VERSION = 1
VALID_FILE_MODES = frozenset({"100644", "100755"})
VALID_OPERATIONS = frozenset({"add", "modify", "delete"})
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
DEFAULT_FORBIDDEN_BASENAMES = frozenset({"handoff.json"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

FileInput = str | os.PathLike[str] | BinaryIO | TextIO


class CommitBridgeError(RuntimeError):
    """Base class for fail-closed bridge errors."""


class HandoffValidationError(CommitBridgeError):
    """The handoff manifest is missing, malformed, or inconsistent."""


class CheckpointValidationError(CommitBridgeError):
    """The checkpoint archive is unsafe or does not match the handoff."""


class RepositoryStateError(CommitBridgeError):
    """Remote repository state does not permit a safe publication."""


class GitHubPublicationError(CommitBridgeError):
    """GitHub object publication failed or returned inconsistent data."""


@dataclass(frozen=True)
class BridgeLimits:
    """Resource and scope limits applied before any GitHub write."""

    max_handoff_bytes: int = 1 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_archive_entries: int = 100_000
    max_changes: int = 256
    max_single_file_bytes: int = 100 * 1024 * 1024
    max_selected_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: float = 1_000.0

    def __post_init__(self) -> None:
        integer_fields = (
            "max_handoff_bytes",
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
class HandoffFile:
    """One explicitly approved repository change."""

    path: str
    operation: str
    mode: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    git_blob_sha: str | None = None


@dataclass(frozen=True)
class HandoffManifest:
    """Machine-readable commit section embedded in ``handoff.json``."""

    repository: str
    base_sha: str
    workspace_root: str
    files: tuple[HandoffFile, ...]
    direct_dependencies: tuple[str, ...] = ()
    issue_number: int | None = None
    forbidden_prefixes: tuple[str, ...] = DEFAULT_FORBIDDEN_PREFIXES
    forbidden_suffixes: tuple[str, ...] = DEFAULT_FORBIDDEN_SUFFIXES
    forbidden_basenames: frozenset[str] = DEFAULT_FORBIDDEN_BASENAMES


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
class PullRequestInfo:
    number: int
    url: str | None = None


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
    """Verified result of one bridge invocation."""

    repository: str
    requested_base_sha: str
    effective_base_sha: str
    base_rebased: bool
    base_branch: str
    branch_name: str
    tree_sha: str
    commit_sha: str
    changed_paths: tuple[str, ...]
    uploaded_blob_shas: Mapping[str, str] = field(default_factory=dict)
    ignored_archive_entries: int = 0
    branch_created: bool = False
    branch_updated: bool = False
    commit_reused: bool = False
    pr_number: int | None = None
    pr_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "requested_base_sha": self.requested_base_sha,
            "effective_base_sha": self.effective_base_sha,
            "base_rebased": self.base_rebased,
            "base_branch": self.base_branch,
            "branch_name": self.branch_name,
            "tree_sha": self.tree_sha,
            "commit_sha": self.commit_sha,
            "changed_paths": list(self.changed_paths),
            "uploaded_blob_shas": dict(self.uploaded_blob_shas),
            "ignored_archive_entries": self.ignored_archive_entries,
            "branch_created": self.branch_created,
            "branch_updated": self.branch_updated,
            "commit_reused": self.commit_reused,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
        }


@runtime_checkable
class GitHubCommitClient(Protocol):
    """Minimal GitHub object API required by the bridge."""

    def get_repository(self, repository: str) -> RepositoryInfo: ...

    def get_branch_head(self, repository: str, branch: str) -> str | None: ...

    def get_commit(self, repository: str, commit_sha: str) -> CommitInfo: ...

    def get_path(self, repository: str, path: str, ref: str) -> RemotePath | None: ...

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

    def compare(self, repository: str, base: str, head: str) -> Sequence[ComparedFile]: ...

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
