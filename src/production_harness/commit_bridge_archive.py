"""Checkpoint ZIP validation and approved-blob extraction."""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .commit_bridge_handoff import _is_forbidden_path
from .commit_bridge_types import (
    BridgeLimits, CheckpointValidationError, FileInput, HandoffManifest,
    PreparedBlob, git_blob_sha,
)

def _zip_member_path(workspace_root: str, repository_path: str) -> str:
    return f"{workspace_root}/{repository_path}" if workspace_root else repository_path


def _archive_to_seekable(
    source: FileInput,
    limits: BridgeLimits,
) -> tuple[BinaryIO, BinaryIO | None]:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        handle: BinaryIO | None = None
        try:
            handle = path.open("rb")
            size = os.fstat(handle.fileno()).st_size
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise CheckpointValidationError(f"cannot open checkpoint_zip: {exc}") from exc
        if size > limits.max_archive_bytes:
            handle.close()
            raise CheckpointValidationError(
                f"checkpoint_zip exceeds max_archive_bytes={limits.max_archive_bytes}"
            )
        return handle, handle

    stream = source
    try:
        if stream.seekable():
            current = stream.tell()
            stream.seek(0, io.SEEK_END)
            size = stream.tell()
            stream.seek(current)
            if size > limits.max_archive_bytes:
                raise CheckpointValidationError(
                    f"checkpoint_zip exceeds max_archive_bytes={limits.max_archive_bytes}"
                )
            return stream, None
    except (AttributeError, OSError):
        pass

    temporary = tempfile.TemporaryFile()
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            temporary.close()
            raise CheckpointValidationError("checkpoint_zip stream must return bytes")
        total += len(chunk)
        if total > limits.max_archive_bytes:
            temporary.close()
            raise CheckpointValidationError(
                f"checkpoint_zip exceeds max_archive_bytes={limits.max_archive_bytes}"
            )
        temporary.write(chunk)
    temporary.seek(0)
    return temporary, temporary


def _validate_zip_name(raw: str) -> str:
    if not raw or "\0" in raw or "\\" in raw:
        raise CheckpointValidationError(f"invalid ZIP member name: {raw!r}")
    if unicodedata.normalize("NFC", raw) != raw:
        raise CheckpointValidationError(f"ZIP member name must be Unicode NFC: {raw!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise CheckpointValidationError(f"absolute ZIP member path: {raw!r}")
    if "//" in raw:
        raise CheckpointValidationError(f"unsafe ZIP member path: {raw!r}")
    trimmed = raw.rstrip("/")
    raw_parts = trimmed.split("/")
    path = PurePosixPath(trimmed)
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CheckpointValidationError(f"unsafe ZIP member path: {raw!r}")
    return path.as_posix()


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return (unix_mode & 0o170000) == 0o120000


def prepare_checkpoint_blobs(
    checkpoint_zip: FileInput,
    manifest: HandoffManifest,
    *,
    limits: BridgeLimits = BridgeLimits(),
) -> tuple[tuple[PreparedBlob, ...], int]:
    """Read only approved files from the ZIP and verify all declared digests."""
    forbidden = [item.path for item in manifest.files if _is_forbidden_path(item.path, manifest)]
    if forbidden:
        raise CheckpointValidationError(f"forbidden publish paths: {forbidden}")
    archive, temporary = _archive_to_seekable(checkpoint_zip, limits)
    try:
        try:
            zip_handle = zipfile.ZipFile(archive, "r")
        except (OSError, TypeError, zipfile.BadZipFile) as exc:
            raise CheckpointValidationError(f"invalid checkpoint_zip: {exc}") from exc
        with zip_handle:
            infos = zip_handle.infolist()
            if len(infos) > limits.max_archive_entries:
                raise CheckpointValidationError(
                    f"checkpoint_zip exceeds max_archive_entries={limits.max_archive_entries}"
                )
            by_name: dict[str, zipfile.ZipInfo] = {}
            casefolded: dict[str, str] = {}
            for info in infos:
                normalized = _validate_zip_name(info.filename)
                if normalized in by_name:
                    raise CheckpointValidationError(f"duplicate ZIP member: {normalized}")
                folded = normalized.casefold()
                previous = casefolded.get(folded)
                if previous is not None and previous != normalized:
                    raise CheckpointValidationError(
                        f"case-colliding ZIP members: {previous!r}, {normalized!r}"
                    )
                casefolded[folded] = normalized
                by_name[normalized] = info

            prepared: list[PreparedBlob] = []
            selected_bytes = 0
            selected_members: set[str] = set()
            for item in manifest.files:
                member = _zip_member_path(manifest.workspace_root, item.path)
                if item.operation == "delete":
                    if member in by_name and not by_name[member].is_dir():
                        raise CheckpointValidationError(
                            f"delete path must be absent from checkpoint payload: {item.path}"
                        )
                    continue
                info = by_name.get(member)
                if info is None or info.is_dir():
                    raise CheckpointValidationError(f"checkpoint is missing approved file: {item.path}")
                if info.flag_bits & 0x1:
                    raise CheckpointValidationError(f"encrypted ZIP member is forbidden: {member}")
                if _is_zip_symlink(info):
                    raise CheckpointValidationError(f"ZIP symlink is forbidden in v1: {member}")
                if info.file_size > limits.max_single_file_bytes:
                    raise CheckpointValidationError(
                        f"{item.path} exceeds max_single_file_bytes={limits.max_single_file_bytes}"
                    )
                if info.compress_size == 0:
                    ratio = float("inf") if info.file_size else 1.0
                else:
                    ratio = info.file_size / info.compress_size
                if ratio > limits.max_compression_ratio:
                    raise CheckpointValidationError(
                        f"{item.path} exceeds max_compression_ratio={limits.max_compression_ratio}"
                    )
                selected_bytes += info.file_size
                if selected_bytes > limits.max_selected_bytes:
                    raise CheckpointValidationError(
                        f"selected payload exceeds max_selected_bytes={limits.max_selected_bytes}"
                    )
                try:
                    data = zip_handle.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise CheckpointValidationError(f"cannot read {member}: {exc}") from exc
                if len(data) > limits.max_single_file_bytes:
                    raise CheckpointValidationError(
                        f"{item.path} exceeds max_single_file_bytes={limits.max_single_file_bytes}"
                    )
                actual_sha256 = hashlib.sha256(data).hexdigest()
                actual_blob_sha = git_blob_sha(data)
                if len(data) != item.size_bytes:
                    raise CheckpointValidationError(
                        f"size mismatch for {item.path}: expected {item.size_bytes}, got {len(data)}"
                    )
                if actual_sha256 != item.sha256:
                    raise CheckpointValidationError(
                        f"SHA-256 mismatch for {item.path}: expected {item.sha256}, got {actual_sha256}"
                    )
                if actual_blob_sha != item.git_blob_sha:
                    raise CheckpointValidationError(
                        f"Git blob SHA mismatch for {item.path}: expected {item.git_blob_sha}, got {actual_blob_sha}"
                    )
                selected_members.add(member)
                prepared.append(
                    PreparedBlob(
                        specification=item,
                        data=data,
                        sha256=actual_sha256,
                        git_blob_sha=actual_blob_sha,
                        zip_member=member,
                    )
                )
            ignored_entries = sum(
                1 for name, info in by_name.items() if not info.is_dir() and name not in selected_members
            )
            return tuple(prepared), ignored_entries
    finally:
        if temporary is not None:
            temporary.close()
