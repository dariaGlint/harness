"""Append-only, hash-chained evidence ledger primitives."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .state import atomic_write_json, load_json_object

LEDGER_SCHEMA_VERSION = 1
EMPTY_LEDGER_HASH: str | None = None
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_MAX_EVENT_BYTES = 1_048_576
_MAX_LEDGER_BYTES = 67_108_864


class LedgerError(RuntimeError):
    """Base class for evidence-ledger failures."""


class LedgerValidationError(LedgerError):
    """Raised when a ledger or event is malformed or tampered with."""


class LedgerLockError(LedgerError):
    """Raised when another writer owns the ledger lock."""


class StaleLedgerError(LedgerError):
    """Raised when a writer's expected sequence or previous hash is stale."""


class EvidenceVerificationError(LedgerValidationError):
    """Raised when a referenced evidence file is missing or changed."""


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
        raise LedgerValidationError(
            f"value is not canonical JSON data: {exc}"
        ) from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LedgerValidationError("timestamp datetime must be timezone-aware")
    normalized = current.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_evidence_path(path: str) -> str:
    """Return one safe, normalized repository-style relative path."""
    if not isinstance(path, str) or not path:
        raise LedgerValidationError("evidence path must be a non-empty string")
    if "\\" in path or "\x00" in path:
        raise LedgerValidationError(
            "evidence path must use normalized POSIX separators"
        )
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(
        part in ("", ".", "..") for part in candidate.parts
    ):
        raise LedgerValidationError(f"unsafe evidence path: {path!r}")
    normalized = candidate.as_posix()
    if normalized != path:
        raise LedgerValidationError(f"evidence path is not normalized: {path!r}")
    return normalized


@dataclass(frozen=True)
class EvidenceReference:
    role: str
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise LedgerValidationError("evidence role must be a non-empty string")
        if self.role != self.role.strip() or len(self.role) > 128:
            raise LedgerValidationError(
                "evidence role must be trimmed and at most 128 characters"
            )
        object.__setattr__(self, "path", normalize_evidence_path(self.path))
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise LedgerValidationError(
                "evidence size_bytes must be a non-negative integer"
            )
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(
            self.sha256
        ):
            raise LedgerValidationError(
                "evidence sha256 must be 64 lowercase hexadecimal characters"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceReference":
        if set(value) != {"role", "path", "size_bytes", "sha256"}:
            raise LedgerValidationError(
                "evidence reference has missing or unexpected fields"
            )
        return cls(
            role=value["role"],
            path=value["path"],
            size_bytes=value["size_bytes"],
            sha256=value["sha256"],
        )


@dataclass(frozen=True)
class LedgerEvent:
    schema_version: int
    sequence: int
    timestamp: str
    event_type: str
    subject_id: str
    actor: str | None
    payload: dict[str, Any]
    evidence: tuple[EvidenceReference, ...]
    previous_hash: str | None
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "event_hash": self.event_hash,
            "event_type": self.event_type,
            "evidence": [item.to_dict() for item in self.evidence],
            "payload": self.payload,
            "previous_hash": self.previous_hash,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "subject_id": self.subject_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class LedgerVerification:
    event_count: int
    first_hash: str | None
    last_hash: str | None
    subject_id: str | None
    event_types: tuple[str, ...]
    verified_evidence_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "event_types": list(self.event_types),
            "first_hash": self.first_hash,
            "last_hash": self.last_hash,
            "subject_id": self.subject_id,
            "verified_evidence_count": self.verified_evidence_count,
        }


def _regular_file_digest(root_path: Path, normalized: str) -> tuple[int, str]:
    if root_path.is_symlink():
        raise EvidenceVerificationError("evidence root must not be a symlink")
    try:
        root = root_path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceVerificationError(
            f"evidence root is missing: {root_path}"
        ) from exc
    if not root.is_dir():
        raise EvidenceVerificationError("evidence root must be a directory")

    candidate = root
    parts = PurePosixPath(normalized).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise EvidenceVerificationError(
                f"evidence file is missing: {normalized}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise EvidenceVerificationError(
                f"evidence path contains a symlink: {normalized}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceVerificationError(
                f"evidence path parent is not a directory: {normalized}"
            )

    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise EvidenceVerificationError(
            f"evidence path escapes root: {normalized}"
        ) from exc

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise EvidenceVerificationError(
            f"evidence file cannot be opened safely: {normalized}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceVerificationError(
                f"evidence path is not a regular file: {normalized}"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or size != after.st_size:
            raise EvidenceVerificationError(
                f"evidence file changed while hashing: {normalized}"
            )
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def create_evidence_reference(
    evidence_root: Path,
    relative_path: str,
    *,
    role: str,
) -> EvidenceReference:
    """Bind one regular, non-symlink evidence file by size and SHA-256."""
    normalized = normalize_evidence_path(relative_path)
    size, digest = _regular_file_digest(Path(evidence_root), normalized)
    return EvidenceReference(
        role=role,
        path=normalized,
        size_bytes=size,
        sha256=digest,
    )


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise LedgerValidationError(
            "timestamp must be an RFC3339 UTC value ending in Z"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerValidationError("timestamp is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LedgerValidationError("timestamp must be UTC")
    return value


def _parse_event(
    value: Mapping[str, Any],
    *,
    expected_sequence: int,
    expected_previous_hash: str | None,
) -> LedgerEvent:
    required = {
        "actor",
        "event_hash",
        "event_type",
        "evidence",
        "payload",
        "previous_hash",
        "schema_version",
        "sequence",
        "subject_id",
        "timestamp",
    }
    if set(value) != required:
        raise LedgerValidationError("ledger event has missing or unexpected fields")
    if value["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise LedgerValidationError("unsupported ledger schema_version")
    sequence = value["sequence"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence != expected_sequence
    ):
        raise LedgerValidationError(
            f"ledger sequence mismatch: expected {expected_sequence}, got {sequence!r}"
        )
    previous_hash = value["previous_hash"]
    if previous_hash != expected_previous_hash:
        raise LedgerValidationError(
            "ledger previous_hash does not match the chain"
        )
    if previous_hash is not None and (
        not isinstance(previous_hash, str)
        or not _SHA256_RE.fullmatch(previous_hash)
    ):
        raise LedgerValidationError("previous_hash is invalid")
    event_hash = value["event_hash"]
    if not isinstance(event_hash, str) or not _SHA256_RE.fullmatch(event_hash):
        raise LedgerValidationError("event_hash is invalid")
    event_type = value["event_type"]
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
        raise LedgerValidationError("event_type is invalid")
    subject_id = value["subject_id"]
    if (
        not isinstance(subject_id, str)
        or not subject_id.strip()
        or subject_id != subject_id.strip()
    ):
        raise LedgerValidationError(
            "subject_id must be a trimmed non-empty string"
        )
    if len(subject_id) > 256:
        raise LedgerValidationError("subject_id is too long")
    actor = value["actor"]
    if actor is not None and (
        not isinstance(actor, str)
        or not actor.strip()
        or actor != actor.strip()
        or len(actor) > 256
    ):
        raise LedgerValidationError(
            "actor must be null or a trimmed non-empty string"
        )
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise LedgerValidationError("payload must be a JSON object")
    _canonical_json(payload)
    evidence_raw = value["evidence"]
    if not isinstance(evidence_raw, list):
        raise LedgerValidationError("evidence must be a JSON array")
    evidence = tuple(
        EvidenceReference.from_mapping(item)
        for item in evidence_raw
        if isinstance(item, dict)
    )
    if len(evidence) != len(evidence_raw):
        raise LedgerValidationError("evidence entries must be JSON objects")
    if tuple(sorted(evidence, key=lambda item: (item.role, item.path))) != evidence:
        raise LedgerValidationError(
            "evidence entries must be sorted by role and path"
        )
    if len({(item.role, item.path) for item in evidence}) != len(evidence):
        raise LedgerValidationError(
            "duplicate evidence role/path pairs are not allowed"
        )
    timestamp = _validate_timestamp(value["timestamp"])

    unsigned = dict(value)
    unsigned.pop("event_hash")
    if _event_hash(unsigned) != event_hash:
        raise LedgerValidationError("event_hash does not match event content")

    return LedgerEvent(
        schema_version=LEDGER_SCHEMA_VERSION,
        sequence=sequence,
        timestamp=timestamp,
        event_type=event_type,
        subject_id=subject_id,
        actor=actor,
        payload=dict(payload),
        evidence=evidence,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )


def load_ledger_events(
    path: Path,
    *,
    max_event_bytes: int = _MAX_EVENT_BYTES,
    max_ledger_bytes: int = _MAX_LEDGER_BYTES,
) -> tuple[LedgerEvent, ...]:
    """Load and fully verify the canonical JSONL hash chain."""
    ledger = Path(path)
    if not ledger.exists():
        return ()
    if not ledger.is_file() or ledger.is_symlink():
        raise LedgerValidationError("ledger path must be a regular file")
    size = ledger.stat().st_size
    if size > max_ledger_bytes:
        raise LedgerValidationError("ledger exceeds the configured size limit")
    raw = ledger.read_bytes()
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise LedgerValidationError(
            "ledger ends with a partial or truncated record"
        )

    events: list[LedgerEvent] = []
    previous_hash: str | None = None
    subject_id: str | None = None
    for index, raw_line in enumerate(raw.splitlines(keepends=True), start=1):
        if len(raw_line) > max_event_bytes:
            raise LedgerValidationError(
                f"ledger event {index} exceeds the size limit"
            )
        if raw_line in (b"\n", b"\r\n"):
            raise LedgerValidationError(f"ledger event {index} is blank")
        try:
            line = raw_line[:-1].decode("utf-8")
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerValidationError(
                f"ledger event {index} is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(value, dict):
            raise LedgerValidationError(
                f"ledger event {index} must be a JSON object"
            )
        event = _parse_event(
            value,
            expected_sequence=index,
            expected_previous_hash=previous_hash,
        )
        canonical_line = _canonical_json(event.to_dict()).encode("utf-8") + b"\n"
        if canonical_line != raw_line:
            raise LedgerValidationError(
                f"ledger event {index} is not canonically encoded"
            )
        if subject_id is None:
            subject_id = event.subject_id
        elif event.subject_id != subject_id:
            raise LedgerValidationError(
                "one ledger cannot mix multiple subject_id values"
            )
        events.append(event)
        previous_hash = event.event_hash
    return tuple(events)


def _verify_evidence(reference: EvidenceReference, evidence_root: Path) -> None:
    actual = create_evidence_reference(
        evidence_root,
        reference.path,
        role=reference.role,
    )
    if (
        actual.size_bytes != reference.size_bytes
        or actual.sha256 != reference.sha256
    ):
        raise EvidenceVerificationError(
            f"evidence file changed: {reference.path}"
        )


def verify_ledger(
    path: Path,
    *,
    evidence_root: Path | None = None,
    expected_subject_id: str | None = None,
    required_event_types: Iterable[str] = (),
    expected_event_count: int | None = None,
    expected_last_hash: str | None = None,
    allow_empty: bool = False,
) -> LedgerVerification:
    """Verify chain integrity and optional evidence and trusted head anchors."""
    events = load_ledger_events(path)
    if not events and not allow_empty:
        raise LedgerValidationError("ledger is empty")
    subject_id = events[0].subject_id if events else None
    if expected_subject_id is not None and subject_id != expected_subject_id:
        raise LedgerValidationError(
            f"ledger subject mismatch: expected {expected_subject_id!r}, "
            f"got {subject_id!r}"
        )
    if expected_event_count is not None:
        if (
            isinstance(expected_event_count, bool)
            or not isinstance(expected_event_count, int)
            or expected_event_count < 0
        ):
            raise LedgerValidationError(
                "expected_event_count must be a non-negative integer"
            )
        if len(events) != expected_event_count:
            raise LedgerValidationError(
                f"ledger event count mismatch: expected {expected_event_count}, "
                f"got {len(events)}"
            )
    if expected_last_hash is not None:
        if not isinstance(expected_last_hash, str) or not _SHA256_RE.fullmatch(
            expected_last_hash
        ):
            raise LedgerValidationError("expected_last_hash is invalid")
        actual_last_hash = events[-1].event_hash if events else None
        if actual_last_hash != expected_last_hash:
            raise LedgerValidationError("ledger last hash does not match trusted anchor")

    event_types = tuple(event.event_type for event in events)
    required = tuple(required_event_types)
    invalid_required = [
        item
        for item in required
        if not isinstance(item, str) or not _EVENT_TYPE_RE.fullmatch(item)
    ]
    if invalid_required:
        raise LedgerValidationError(
            f"invalid required event types: {invalid_required}"
        )
    missing = sorted(set(required) - set(event_types))
    if missing:
        raise LedgerValidationError(f"required event types are missing: {missing}")

    verified_evidence = 0
    if evidence_root is not None:
        for event in events:
            for reference in event.evidence:
                _verify_evidence(reference, Path(evidence_root))
                verified_evidence += 1
    return LedgerVerification(
        event_count=len(events),
        first_hash=events[0].event_hash if events else None,
        last_hash=events[-1].event_hash if events else None,
        subject_id=subject_id,
        event_types=event_types,
        verified_evidence_count=verified_evidence,
    )


class _LedgerLock:
    def __init__(self, ledger: Path) -> None:
        self.path = Path(f"{ledger}.lock")
        self.fd: int | None = None

    def __enter__(self) -> "_LedgerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise LedgerLockError(
                f"ledger writer lock already exists: {self.path}"
            ) from exc
        payload = {"pid": os.getpid(), "created_at": _utc_timestamp()}
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        os.write(self.fd, encoded)
        os.fsync(self.fd)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _rollback_append(ledger: Path, old_size: int, existed_before: bool) -> None:
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger, flags)
    try:
        os.ftruncate(descriptor, old_size)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if not existed_before and old_size == 0:
        ledger.unlink(missing_ok=True)
    _fsync_directory(ledger.parent)


def append_ledger_event(
    path: Path,
    *,
    event_type: str,
    subject_id: str,
    payload: Mapping[str, Any],
    evidence: Sequence[EvidenceReference | Mapping[str, Any]] = (),
    actor: str | None = None,
    timestamp: datetime | None = None,
    expected_sequence: int | None = None,
    expected_previous_hash: str | None = None,
) -> LedgerEvent:
    """Append one event under an exclusive writer lock and verify the result."""
    ledger = Path(path)
    with _LedgerLock(ledger):
        events = load_ledger_events(ledger)
        existed_before = ledger.exists()
        old_size = ledger.stat().st_size if existed_before else 0
        next_sequence = len(events) + 1
        previous_hash = events[-1].event_hash if events else None
        if events and events[0].subject_id != subject_id:
            raise StaleLedgerError("subject_id does not match the existing ledger")
        if expected_sequence is not None and expected_sequence != next_sequence:
            raise StaleLedgerError(
                f"stale ledger sequence: expected next {expected_sequence}, "
                f"actual {next_sequence}"
            )
        if (
            expected_previous_hash is not None
            and expected_previous_hash != previous_hash
        ):
            raise StaleLedgerError("stale ledger previous hash")
        if not isinstance(payload, Mapping):
            raise LedgerValidationError("payload must be a mapping")
        references = tuple(
            item
            if isinstance(item, EvidenceReference)
            else EvidenceReference.from_mapping(item)
            for item in evidence
        )
        references = tuple(
            sorted(references, key=lambda item: (item.role, item.path))
        )
        if len({(item.role, item.path) for item in references}) != len(references):
            raise LedgerValidationError(
                "duplicate evidence role/path pairs are not allowed"
            )
        unsigned = {
            "actor": actor,
            "event_type": event_type,
            "evidence": [item.to_dict() for item in references],
            "payload": dict(payload),
            "previous_hash": previous_hash,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "sequence": next_sequence,
            "subject_id": subject_id,
            "timestamp": _utc_timestamp(timestamp),
        }
        event_value = dict(unsigned)
        event_value["event_hash"] = _event_hash(unsigned)
        event = _parse_event(
            event_value,
            expected_sequence=next_sequence,
            expected_previous_hash=previous_hash,
        )
        encoded = (_canonical_json(event.to_dict()) + "\n").encode("utf-8")
        if len(encoded) > _MAX_EVENT_BYTES:
            raise LedgerValidationError("ledger event exceeds the size limit")
        if old_size + len(encoded) > _MAX_LEDGER_BYTES:
            raise LedgerValidationError("ledger exceeds the configured size limit")

        ledger.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(ledger, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise LedgerValidationError("ledger path must be a regular file")
            if metadata.st_size != old_size:
                raise StaleLedgerError("ledger changed after lock acquisition")
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise LedgerError("ledger append made no progress")
                offset += written
            os.fsync(descriptor)
        except Exception:
            try:
                os.ftruncate(descriptor, old_size)
                os.fsync(descriptor)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
        _fsync_directory(ledger.parent)

        try:
            verified = load_ledger_events(ledger)
            if not verified or verified[-1].event_hash != event.event_hash:
                raise LedgerError("ledger post-append verification failed")
        except Exception as exc:
            try:
                _rollback_append(ledger, old_size, existed_before)
            except OSError as rollback_error:
                raise LedgerError(
                    "ledger post-append verification and rollback failed: "
                    f"{rollback_error}"
                ) from exc
            raise
        return event


def ledger_snapshot(path: Path) -> dict[str, Any]:
    """Return a trusted head anchor suitable for another durable report."""
    verification = verify_ledger(path)
    payload = verification.to_dict()
    payload["schema_version"] = LEDGER_SCHEMA_VERSION
    payload["ledger_path"] = str(Path(path))
    return payload


def write_ledger_snapshot(path: Path, snapshot_path: Path) -> None:
    """Persist a verified ledger head anchor atomically."""
    atomic_write_json(Path(snapshot_path), ledger_snapshot(Path(path)))


def verify_ledger_against_snapshot(
    path: Path,
    snapshot_path: Path,
    *,
    evidence_root: Path | None = None,
    required_event_types: Iterable[str] = (),
) -> LedgerVerification:
    """Verify a ledger against an externally retained trusted head snapshot."""
    snapshot = load_json_object(Path(snapshot_path))
    if snapshot is None:
        raise LedgerValidationError("ledger snapshot is missing or invalid")
    if snapshot.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise LedgerValidationError("unsupported ledger snapshot schema_version")
    event_count = snapshot.get("event_count")
    last_hash = snapshot.get("last_hash")
    subject_id = snapshot.get("subject_id")
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 1
    ):
        raise LedgerValidationError("ledger snapshot event_count is invalid")
    if not isinstance(last_hash, str) or not _SHA256_RE.fullmatch(last_hash):
        raise LedgerValidationError("ledger snapshot last_hash is invalid")
    if not isinstance(subject_id, str) or not subject_id:
        raise LedgerValidationError("ledger snapshot subject_id is invalid")
    return verify_ledger(
        path,
        evidence_root=evidence_root,
        expected_subject_id=subject_id,
        required_event_types=required_event_types,
        expected_event_count=event_count,
        expected_last_hash=last_hash,
    )
