from __future__ import annotations

from dataclasses import replace
import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "production_harness"
if "production_harness" not in sys.modules:
    package = types.ModuleType("production_harness")
    package.__path__ = [str(PACKAGE_ROOT)]
    package.__package__ = "production_harness"
    sys.modules["production_harness"] = package

acceptance = importlib.import_module("production_harness.operational_acceptance")


class OperationalAcceptanceMutabilityTests(unittest.TestCase):
    def test_contract_policy_is_immutable_and_digest_bound(self) -> None:
        contract = acceptance.build_acceptance_contract(
            subject_id="task",
            gates=[acceptance.AcceptanceGate("optional", required=False)],
            failed_optional="fail",
        )
        with self.assertRaises(TypeError):
            contract.verdict_policy["failed_optional"] = "ignore"
        with self.assertRaisesRegex(
            acceptance.AcceptanceValidationError,
            "digest",
        ):
            replace(
                contract,
                verdict_policy={
                    **dict(contract.verdict_policy),
                    "failed_optional": "ignore",
                },
            )


if __name__ == "__main__":
    unittest.main()
