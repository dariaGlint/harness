from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from commit_bridge_support import *


class CommitBridgeValidationTests(unittest.TestCase):
    def test_small_zip_reads_exact_bytes(self) -> None:
        data = b"func run():\n\tpass\n"
        files = [file_spec("scripts/small.gd", "add", data)]
        manifest = load_handoff_manifest(io.BytesIO(handoff_bytes(files)))
        prepared = prepare_checkpoint_blobs(
            io.BytesIO(checkpoint_bytes({"workspace/scripts/small.gd": data})), manifest
        )
        self.assertEqual(prepared[0].data, data)

    def test_50kb_text_is_not_truncated(self) -> None:
        data = (b"func tick():\n\tpass\n" * 3200)
        self.assertGreater(len(data), 50 * 1024)
        result, client = publish_one(data, path="scripts/large.gd")
        self.assertEqual(result.status, "success")
        self.assertEqual(client.uploaded_data, [data])

    def test_500kb_file_uses_path_references(self) -> None:
        data = (b"func tick():\n\tpass\n" * 30000)
        self.assertGreater(len(data), 500 * 1024)
        files = [file_spec("scripts/huge.gd", "add", data)]
        client = FakeClient()
        client.compare_result = success_compare(files)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkpoint = root / "checkpoint.zip"
            handoff = root / "handoff.json"
            checkpoint.write_bytes(checkpoint_bytes({"workspace/scripts/huge.gd": data}))
            handoff.write_bytes(handoff_bytes(files))
            result = commit_checkpoint_to_github(
                "owner/repo", BASE, "agent/huge", "Add huge script",
                checkpoint, handoff, client=client,
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(client.uploaded_data, [data])

    def test_path_traversal_is_rejected(self) -> None:
        result, client = publish_one(checkpoint_extra={"../escape.txt": b"bad"})
        self.assertEqual((result.status, result.reason), ("rejected", "unsafe_zip_path"))
        self.assertNotIn("create_blob", client.calls)

    def test_absolute_path_is_rejected(self) -> None:
        files = [file_spec("/absolute.gd", "add", b"x")]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/absolute", "test",
            io.BytesIO(checkpoint_bytes({"/absolute.gd": b"x"})),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "invalid_path")

    def test_symlink_is_rejected(self) -> None:
        data = b"target"
        files = [file_spec("scripts/link.gd", "add", data)]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/symlink", "test",
            io.BytesIO(symlink_checkpoint("workspace/scripts/link.gd", "target")),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "zip_symlink")

    def test_duplicate_normalized_path_is_rejected(self) -> None:
        data = b"x"
        files = [file_spec("scripts/a.gd", "add", data)]
        archive = checkpoint_bytes([
            ("workspace/scripts/a.gd", data),
            ("workspace/scripts/a.gd", data),
        ])
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/duplicate", "test",
            io.BytesIO(archive), io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "duplicate_zip_path")

    def test_handoff_extra_file_is_rejected(self) -> None:
        result, client = publish_one(checkpoint_extra={"workspace/other.txt": b"extra"})
        self.assertEqual(result.reason, "unexpected_archive_file")
        self.assertNotIn("create_blob", client.calls)

    def test_required_file_missing_is_rejected(self) -> None:
        data = b"x"
        files = [file_spec("scripts/missing.gd", "add", data)]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/missing", "test",
            io.BytesIO(checkpoint_bytes({})), io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "required_file_missing")

    def test_sha256_mismatch_is_rejected(self) -> None:
        files = [file_spec("scripts/a.gd", "add", b"expected")]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/digest", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/a.gd": b"actual"})),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "size_mismatch")

    def test_forbidden_artifacts_are_rejected(self) -> None:
        for path in (
            "captures/run.mp4",
            "validation_output/result.json",
            "movie_frames/0001.png",
            "handoff.json",
            ".godot/editor.log",
            "logs/validation.log",
            "checkpoint.zip",
        ):
            with self.subTest(path=path):
                result, _ = publish_one(b"x", path=path)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.stage, "checkpoint_validation")

    def test_game_asset_image_requires_allowed_path(self) -> None:
        data = b"\x89PNG\r\n"
        policy = {
            "schema_version": 1,
            "repository": "owner/repo",
            "allowed_branch_prefixes": ["agent/"],
            "allowed_asset_patterns": ["assets/**"],
        }
        result, _ = publish_one(data, path="assets/icons/player.png", policy_json=io.BytesIO(json.dumps(policy).encode()))
        self.assertEqual(result.status, "success")
        rejected, _ = publish_one(data, path="screens/player.png", policy_json=io.BytesIO(json.dumps(policy).encode()))
        self.assertEqual(rejected.reason, "forbidden_publish_path")

    def test_verification_image_is_rejected_inside_asset_path(self) -> None:
        data = b"\x89PNG\r\n"
        policy = {
            "schema_version": 1,
            "allowed_branch_prefixes": ["agent/"],
            "allowed_asset_patterns": ["assets/**"],
        }
        result, _ = publish_one(data, path="assets/review_preview.png", policy_json=io.BytesIO(json.dumps(policy).encode()))
        self.assertEqual(result.reason, "forbidden_publish_path")

    def test_unexpected_empty_file_is_rejected(self) -> None:
        files = [file_spec("scripts/empty.gd", "add", b"")]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/empty", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/empty.gd": b""})),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "unexpected_empty_file")

    def test_utf8_corruption_is_rejected(self) -> None:
        data = b"\xff\xfe"
        files = [file_spec("scripts/broken.gd", "add", data, encoding="utf-8")]
        result = commit_checkpoint_to_github(
            "owner/repo", BASE, "agent/utf8", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/broken.gd": data})),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual(result.reason, "utf8_decode_error")

    def test_master_and_invalid_prefix_are_rejected(self) -> None:
        master, _ = publish_one(branch="main")
        self.assertEqual(master.reason, "unauthorized_branch_prefix")
        invalid, _ = publish_one(branch="feature/nope")
        self.assertEqual(invalid.reason, "unauthorized_branch_prefix")

    def test_player_change_requires_preview_approval(self) -> None:
        result, client = publish_one(behavior_change=True, preview_approved=False)
        self.assertEqual(result.reason, "preview_not_approved")
        self.assertNotIn("create_blob", client.calls)

    def test_policy_schema_and_handoff_schema_are_valid_json(self) -> None:
        schema_root = PACKAGE_ROOT / "schemas"
        for name in ("workspace_commit_handoff_v1.json", "workspace_commit_policy_v1.json"):
            payload = json.loads((schema_root / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_policy_cannot_disable_fail_closed_controls(self) -> None:
        policy = {
            "schema_version": 1,
            "allowed_branch_prefixes": ["agent/"],
            "require_compare": False,
        }
        with self.assertRaises(Exception) as captured:
            load_commit_bridge_policy(io.BytesIO(json.dumps(policy).encode()))
        self.assertEqual(getattr(captured.exception, "reason", None), "unsafe_policy_relaxation")

    def test_missing_base_sha_returns_structured_rejection(self) -> None:
        data = b"x"
        files = [file_spec("scripts/a.gd", "add", data)]
        result = commit_checkpoint_to_github(
            "owner/repo", "", "agent/no-base", "test",
            io.BytesIO(checkpoint_bytes({"workspace/scripts/a.gd": data})),
            io.BytesIO(handoff_bytes(files)), client=FakeClient(),
        )
        self.assertEqual((result.status, result.reason), ("rejected", "invalid_git_sha"))


if __name__ == "__main__":
    unittest.main()
