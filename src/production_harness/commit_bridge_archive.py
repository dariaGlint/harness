"""Safe ZIP inspection for Workspace Commit Bridge."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from .commit_bridge_handoff import normalize_repo_path
from .commit_bridge_policy import policy_path_rejection
from .commit_bridge_types import (
    BridgeLimits,
    CheckpointValidationError,
    CommitBridgePolicy,
    FileInput,
    HandoffManifest,
    PreparedBlob,
    git_blob_sha,
)


def _archive_path(source: FileInput, limits: BridgeLimits) -> tuple[Path, tempfile.NamedTemporaryFile | None]:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CheckpointValidationError(
                "checkpoint_unreadable", f"cannot stat checkpoint_zip: {exc}"
            ) from exc
        if size > limits.max_archive_bytes:
            raise CheckpointValidationError(
                "checkpoint_too_large", f"checkpoint_zip exceeds {limits.max_archive_bytes} bytes"
            )
        return path, None
    temporary = tempfile.NamedTemporaryFile(prefix="workspace-commit-bridge-", suffix=".zip")
    total = 0
    try:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if not isinstance(chunk, bytes):
                raise CheckpointValidationError(
                    "checkpoint_unreadable", "checkpoint_zip stream must return bytes"
                )
            total += len(chunk)
            if total > limits.max_archive_bytes:
                raise CheckpointValidationError(
                    "checkpoint_too_large", f"checkpoint_zip exceeds {limits.max_archive_bytes} bytes"
                )
            temporary.write(chunk)
        temporary.flush()
        return Path(temporary.name), temporary
    except Exception:
        temporary.close()
        raise


def _normalized_zip_name(raw: str) -> str:
    if not raw or "\0" in raw or "\\" in raw:
        raise CheckpointValidationError("unsafe_zip_path", f"unsafe ZIP member path: {raw!r}")
    if unicodedata.normalize("NFC", raw) != raw:
        raise CheckpointValidationError(
            "non_normalized_zip_path", f"ZIP member must be Unicode NFC: {raw!r}"
        )
    directory = raw.endswith("/")
    candidate = raw[:-1] if directory else raw
    parts = candidate.split("/")
    if (
        not candidate
        or raw.startswith("/")
        or len(candidate) >= 2
        and candidate[1] == ":"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CheckpointValidationError("unsafe_zip_path", f"unsafe ZIP member path: {raw!r}")
    normalized = PurePosixPath(candidate).as_posix()
    return normalized + ("/" if directory else "")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _member_for(manifest: HandoffManifest, path: str) -> str:
    return f"{manifest.workspace_root}/{path}" if manifest.workspace_root else path


def prepare_checkpoint_blobs(
    checkpoint_zip: FileInput,
    manifest: HandoffManifest,
    *,
    policy: CommitBridgePolicy = CommitBridgePolicy(),
    limits: BridgeLimits = BridgeLimits(),
) -> tuple[PreparedBlob, ...]:
    """Read approved files directly from ZIP without extracting them to the workspace."""
    forbidden: list[str] = []
    for item in manifest.files:
        rejection = policy_path_rejection(item.path, policy)
        if rejection is not None:
            forbidden.append(f"{item.path}: {rejection[1]}")
    if forbidden:
        raise CheckpointValidationError(
            "forbidden_publish_path", "forbidden publish paths: " + repr(forbidden)
        )
    archive_path, temporary = _archive_path(checkpoint_zip, limits)
    try:
        try:
            archive = zipfile.ZipFile(archive_path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise CheckpointValidationError("invalid_checkpoint_zip", f"invalid checkpoint_zip: {exc}") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > limits.max_archive_entries:
                raise CheckpointValidationError(
                    "too_many_archive_entries",
                    f"checkpoint contains more than {limits.max_archive_entries} entries",
                )
            by_name: dict[str, zipfile.ZipInfo] = {}
            folded: dict[str, str] = {}
            for info in infos:
                normalized = _normalized_zip_name(info.filename)
                key = normalized.rstrip("/")
                if _is_symlink(info):
                    raise CheckpointValidationError(
                        "zip_symlink", f"ZIP symlink is forbidden: {info.filename}"
                    )
                if info.flag_bits & 0x1:
                    raise CheckpointValidationError(
                        "encrypted_zip_member", f"encrypted ZIP member is forbidden: {info.filename}"
                    )
                if key in by_name:
                    raise CheckpointValidationError(
                        "duplicate_zip_path", f"duplicate normalized ZIP path: {key}"
                    )
                previous = folded.get(key.casefold())
                if previous is not None:
                    raise CheckpointValidationError(
                        "case_collision", f"case-colliding ZIP paths: {previous!r}, {key!r}"
                    )
                folded[key.casefold()] = key
                by_name[key] = info

            expected_members = {
                _member_for(manifest, item.path): item
                for item in manifest.files
                if item.operation != "delete"
            }
            delete_members = {
                _member_for(manifest, item.path): item
                for item in manifest.files
                if item.operation == "delete"
            }
            file_members = {
                name for name, info in by_name.items() if not info.is_dir()
            }
            present_delete = sorted(set(delete_members) & file_members)
            if present_delete:
                raise CheckpointValidationError(
                    "delete_payload_present",
                    f"delete paths must be absent from checkpoint: {present_delete}",
                )
            missing = sorted(set(expected_members) - file_members)
            if missing:
                raise CheckpointValidationError(
                    "required_file_missing", f"checkpoint is missing approved files: {missing}"
                )
            unexpected = sorted(file_members - set(expected_members))
            if unexpected and policy.reject_unexpected_archive_entries:
                raise CheckpointValidationError(
                    "unexpected_archive_file",
                    f"checkpoint contains files absent from handoff.json: {unexpected}",
                )

            selected_bytes = 0
            prepared: list[PreparedBlob] = []
            for member, item in sorted(expected_members.items()):
                info = by_name[member]
                if info.is_dir():
                    raise CheckpointValidationError(
                        "required_file_missing", f"checkpoint member is a directory: {member}"
                    )
                if info.file_size > limits.max_single_file_bytes:
                    raise CheckpointValidationError(
                        "single_file_too_large",
                        f"{item.path} exceeds max_single_file_bytes={limits.max_single_file_bytes}",
                    )
                ratio = (
                    float("inf") if info.compress_size == 0 and info.file_size else
                    1.0 if info.compress_size == 0 else
                    info.file_size / info.compress_size
                )
                if ratio > limits.max_compression_ratio:
                    raise CheckpointValidationError(
                        "compression_ratio_exceeded",
                        f"{item.path} exceeds max_compression_ratio={limits.max_compression_ratio}",
                    )
                selected_bytes += info.file_size
                if selected_bytes > limits.max_selected_bytes:
                    raise CheckpointValidationError(
                        "selected_payload_too_large",
                        f"selected payload exceeds max_selected_bytes={limits.max_selected_bytes}",
                    )
                try:
                    with archive.open(info, "r") as stream:
                        chunks: list[bytes] = []
                        total = 0
                        digest = hashlib.sha256()
                        while True:
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > limits.max_single_file_bytes:
                                raise CheckpointValidationError(
                                    "single_file_too_large",
                                    f"{item.path} exceeds max_single_file_bytes={limits.max_single_file_bytes}",
                                )
                            digest.update(chunk)
                            chunks.append(chunk)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise CheckpointValidationError(
                        "checkpoint_member_unreadable", f"cannot read {member}: {exc}"
                    ) from exc
                data = b"".join(chunks)
                actual_sha256 = digest.hexdigest()
                actual_blob_sha = git_blob_sha(data)
                if len(data) != item.size_bytes:
                    raise CheckpointValidationError(
                        "size_mismatch",
                        f"size mismatch for {item.path}: expected {item.size_bytes}, got {len(data)}",
                    )
                if actual_sha256 != item.sha256:
                    raise CheckpointValidationError(
                        "sha256_mismatch",
                        f"SHA-256 mismatch for {item.path}: expected {item.sha256}, got {actual_sha256}",
                    )
                if actual_blob_sha != item.git_blob_sha:
                    raise CheckpointValidationError(
                        "git_blob_sha_mismatch",
                        f"Git blob SHA mismatch for {item.path}: expected {item.git_blob_sha}, got {actual_blob_sha}",
                    )
                if not data and policy.reject_empty_files_unless_allowed and not item.allow_empty:
                    raise CheckpointValidationError(
                        "unexpected_empty_file", f"empty file requires allow_empty=true: {item.path}"
                    )
                if item.encoding == "utf-8":
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise CheckpointValidationError(
                            "utf8_decode_error", f"UTF-8 text is corrupt for {item.path}: {exc}"
                        ) from exc
                prepared.append(
                    PreparedBlob(
                        specification=item,
                        data=data,
                        sha256=actual_sha256,
                        git_blob_sha=actual_blob_sha,
                        zip_member=member,
                    )
                )
            return tuple(prepared)
    finally:
        if temporary is not None:
            temporary.close()
