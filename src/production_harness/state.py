"""Durable JSON state primitives used by long-running harnesses."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import AbstractSet, Any, Callable, Mapping


class StateError(RuntimeError):
    """Raised when durable state cannot be resolved safely."""


StateValidator = Callable[[Mapping[str, Any]], bool]


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one JSON object atomically and fsync the file and directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            # Some filesystems and Windows do not support directory fsync.
            pass
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def load_json_object(path: Path) -> dict[str, Any] | None:
    """Return a JSON object, or ``None`` for missing/corrupt/non-object data."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_valid_state_object(
    path: Path,
    *,
    validator: StateValidator | None = None,
) -> dict[str, Any] | None:
    """Load a durable state object and apply an optional fail-closed validator."""
    state = load_json_object(path)
    if state is None:
        return None
    if validator is None:
        return state
    try:
        accepted = validator(state)
    except Exception as exc:  # pragma: no cover - defensive contract boundary
        raise StateError(f"State validator failed for {path}: {exc}") from exc
    return state if accepted else None


def latest_unfinished_task_id(
    state_root: Path,
    *,
    terminal_machine_states: AbstractSet[str],
    state_filename: str = "state.json",
    machine_state_key: str = "machine_state",
    validator: StateValidator | None = None,
) -> str:
    """Return the most recently modified valid, non-terminal task directory."""
    if not state_filename:
        raise StateError("state_filename must not be empty")
    if not machine_state_key:
        raise StateError("machine_state_key must not be empty")

    candidates: list[tuple[float, str]] = []
    root = Path(state_root)
    if root.is_dir():
        for directory in root.iterdir():
            state_path = directory / state_filename
            if not state_path.is_file():
                continue
            state = load_valid_state_object(state_path, validator=validator)
            if state is None:
                continue
            machine_state = state.get(machine_state_key)
            if not isinstance(machine_state, str) or not machine_state:
                continue
            if machine_state in terminal_machine_states:
                continue
            candidates.append((state_path.stat().st_mtime, directory.name))
    if not candidates:
        raise StateError(f"No unfinished task under {root}")
    return max(candidates)[1]
