from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from commit_bridge_support import *


class CommitBridgeTests(unittest.TestCase):
    def test_git_blob_sha_matches_known_value(self) -> None:
        self.assertEqual(git_blob_sha(b"test\n"), "9daeafb9864cf43055ae93beb0afd6c7d144bfa4")

    def test_load_handoff_rejects_case_collision(self) -> None:
        data = b"x"
        with self.assertRaises(HandoffValidationError):
            load_handoff_manifest(
                io.BytesIO(
                    handoff_bytes(
                        [
                            file_spec("A.txt", "add", data),
                            file_spec("a.txt", "add", data),
                        ]
                    )
                )
            )

    def test_load_handoff_rejects_unknown_machine_fields(self) -> None:
        data = b"x"
        payload = json.loads(handoff_bytes([file_spec("safe.txt", "add", data)]))
        payload["commit_bridge"]["unexpected"] = True
        with self.assertRaisesRegex(HandoffValidationError, "unknown commit_bridge fields"):
            load_handoff_manifest(io.BytesIO(json.dumps(payload).encode("utf-8")))

    def test_load_handoff_rejects_unknown_file_fields(self) -> None:
        data = b"x"
        specification = file_spec("safe.txt", "add", data)
        specification["note"] = "not part of the machine contract"
        with self.assertRaisesRegex(HandoffValidationError, "unsupported fields"):
            load_handoff_manifest(io.BytesIO(handoff_bytes([specification])))

    def test_direct_handoff_rejects_unknown_fields(self) -> None:
        payload = json.loads(handoff_bytes([file_spec("safe.txt", "add", b"x")]))[
            "commit_bridge"
        ]
        payload["human_note"] = "use the nested contract for orchestration fields"
        with self.assertRaisesRegex(HandoffValidationError, "unknown direct handoff fields"):
            load_handoff_manifest(io.BytesIO(json.dumps(payload).encode("utf-8")))

    def test_handoff_rejects_conflicting_issue_numbers(self) -> None:
        payload = json.loads(handoff_bytes([file_spec("safe.txt", "add", b"x")]))
        payload["commit_bridge"]["issue_number"] = 8
        with self.assertRaisesRegex(HandoffValidationError, "issue_number values differ"):
            load_handoff_manifest(io.BytesIO(json.dumps(payload).encode("utf-8")))

    def test_checkpoint_rejects_empty_path_components(self) -> None:
        data = b"safe"
        manifest = load_handoff_manifest(
            io.BytesIO(handoff_bytes([file_spec("safe.txt", "add", data)]))
        )
        archive = checkpoint_bytes({"workspace/safe.txt": data, "workspace//ignored.txt": b"bad"})
        with self.assertRaisesRegex(CheckpointValidationError, "unsafe ZIP member path"):
            prepare_checkpoint_blobs(io.BytesIO(archive), manifest)

    def test_checkpoint_rejects_path_traversal(self) -> None:
        data = b"safe"
        manifest = load_handoff_manifest(
            io.BytesIO(handoff_bytes([file_spec("safe.txt", "add", data)]))
        )
        archive = checkpoint_bytes({"workspace/safe.txt": data, "../escape.txt": b"bad"})
        with self.assertRaises(CheckpointValidationError):
            prepare_checkpoint_blobs(io.BytesIO(archive), manifest)

    def test_checkpoint_rejects_digest_mismatch(self) -> None:
        manifest = load_handoff_manifest(
            io.BytesIO(handoff_bytes([file_spec("safe.txt", "add", b"expected")]))
        )
        archive = checkpoint_bytes({"workspace/safe.txt": b"actual"})
        with self.assertRaisesRegex(CheckpointValidationError, "mismatch"):
            prepare_checkpoint_blobs(io.BytesIO(archive), manifest)

    def test_delete_must_be_absent_from_payload(self) -> None:
        manifest = load_handoff_manifest(
            io.BytesIO(
                handoff_bytes([{"path": "old.txt", "operation": "delete"}])
            )
        )
        archive = checkpoint_bytes({"workspace/old.txt": b"still present"})
        with self.assertRaisesRegex(CheckpointValidationError, "must be absent"):
            prepare_checkpoint_blobs(io.BytesIO(archive), manifest)

    def test_path_file_references_support_large_text_without_argument_expansion(self) -> None:
        data = (b"func tick():\n\tpass\n" * 20_000)
        client = FakeClient()
        client.compare_files = [ComparedFile("scripts/large.gd", "added")]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkpoint = root / "checkpoint.zip"
            handoff = root / "handoff.json"
            checkpoint.write_bytes(
                checkpoint_bytes({"workspace/scripts/large.gd": data})
            )
            handoff.write_bytes(
                handoff_bytes([file_spec("scripts/large.gd", "add", data)])
            )
            result = commit_checkpoint_to_github(
                "owner/repo",
                BASE,
                "agent/large-file",
                "Add large script",
                checkpoint,
                handoff,
                client=client,
            )
        self.assertEqual(result.changed_paths, ("scripts/large.gd",))
        self.assertEqual(client.uploaded_data, [data])
        self.assertGreater(len(data), 100_000)



if __name__ == "__main__":
    unittest.main()
