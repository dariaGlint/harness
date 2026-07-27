"""Versioned report contract helpers."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Sequence

REPORT_SCHEMA_VERSION = 1
REPORT_SCHEMA_NAME = "foreground-report-v1.schema.json"
STATE_ENVELOPE_SCHEMA_NAME = "task-state-envelope-v1.schema.json"


def command_template_sha256(arguments: Sequence[str]) -> str:
    """Return a deterministic digest without persisting raw command arguments."""
    encoded = json.dumps(list(arguments), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schema_resource(name: str):
    return files("production_harness.schemas").joinpath(name)


def load_foreground_report_schema() -> Mapping[str, Any]:
    """Load the packaged foreground report JSON Schema."""
    return json.loads(_schema_resource(REPORT_SCHEMA_NAME).read_text(encoding="utf-8"))


def load_task_state_envelope_schema() -> Mapping[str, Any]:
    """Load the default minimal task-state envelope JSON Schema."""
    return json.loads(_schema_resource(STATE_ENVELOPE_SCHEMA_NAME).read_text(encoding="utf-8"))


def foreground_report_schema_path() -> Path:
    """Return the report schema path when the package is installed unpacked."""
    return Path(str(_schema_resource(REPORT_SCHEMA_NAME)))
