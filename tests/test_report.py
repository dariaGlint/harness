from __future__ import annotations

import unittest

from production_harness.report import (
    REPORT_SCHEMA_VERSION,
    command_template_sha256,
    foreground_report_schema_path,
    load_foreground_report_schema,
    load_task_state_envelope_schema,
)


class ReportContractTests(unittest.TestCase):
    def test_packaged_report_schema_is_versioned(self) -> None:
        schema = load_foreground_report_schema()
        self.assertEqual(schema["properties"]["schema_version"]["const"], REPORT_SCHEMA_VERSION)
        self.assertEqual(schema["additionalProperties"], False)
        self.assertTrue(foreground_report_schema_path().is_file())

    def test_default_state_envelope_requires_machine_state(self) -> None:
        schema = load_task_state_envelope_schema()
        self.assertIn("machine_state", schema["required"])
        self.assertEqual(schema["additionalProperties"], True)

    def test_command_template_digest_is_deterministic_and_argument_sensitive(self) -> None:
        first = command_template_sha256(("python", "workflow.py", "{operation}"))
        second = command_template_sha256(("python", "workflow.py", "{operation}"))
        changed = command_template_sha256(("python", "other.py", "{operation}"))
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    unittest.main()
