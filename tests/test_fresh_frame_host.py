from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from host.artifacts import ArtifactStore
from host.providers import ProviderRegistry
from host.service import CommandService
from host.ledger import Ledger
from host.validation import validate_command


class SyntheticFreshFrameProvider:
    def __init__(self, frame_path: Path, *, tamper_hash: bool = False) -> None:
        self.frame_path = frame_path
        self.tamper_hash = tamper_hash
        self.owner_generation: int | None = None
        self.claim_fresh_fact = False

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        frame_bytes = self.frame_path.read_bytes()
        digest = hashlib.sha256(frame_bytes).hexdigest()
        if self.tamper_hash:
            digest = "0" * 64
        result: dict[str, Any] = {
            "taskId": command["taskId"],
            "sessionId": command["context"]["sessionId"],
            "launchId": command["context"]["expectedLaunchId"],
            "runtimeRevision": command["context"]["expectedRuntimeRevision"],
            "ownerGeneration": self.owner_generation
            if self.owner_generation is not None
            else command["arguments"]["expectedOwnerGeneration"],
            "targetId": command["arguments"]["targetId"],
            "freshFrame": {
                "artifactId": command["arguments"]["frameArtifactId"],
                "kind": "screenshot",
                "mediaType": "image/png",
                "path": str(self.frame_path),
                "sha256": digest,
                "size": len(frame_bytes),
                "frame": command["arguments"]["minimumFrameExclusive"] + 1,
                "width": 64,
                "height": 48,
                "fresh": True,
                "runtimeRevision": command["context"]["expectedRuntimeRevision"],
                "viewportGeneration": command["arguments"]["expectedViewportGeneration"],
                "publishedAtUnixMilliseconds": 1,
            },
            "evidence": [
                {
                    "kind": "screenshot",
                    "stage": "verify",
                    "artifactId": command["arguments"]["frameArtifactId"],
                    "sha256": digest,
                    "detail": "synthetic frame",
                }
            ],
        }
        if self.claim_fresh_fact:
            result["freshVerification"] = {
                "sessionId": command["context"]["sessionId"],
                "baselineId": "baseline_synthetic",
                "checkSetId": command["arguments"]["checkSetId"],
                "evidenceArtifactIds": [command["arguments"]["frameArtifactId"]],
            }
        return {
            "status": "completed",
            "result": result,
            "runtimeChanged": False,
            "facts": {"freshVerified": True} if self.claim_fresh_fact else {},
            "artifacts": [],
            "timingsMs": {},
        }


class FreshFrameHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.frame_path = self.artifact_root / "synthetic-frame.png"
        self.frame_path.write_bytes(b"synthetic fresh frame")
        self.ledger = Ledger(self.root / "host.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])
        self.provider = SyntheticFreshFrameProvider(self.frame_path)
        providers = ProviderRegistry()
        providers.register("verification", "synthetic-verifier", self.provider, verified=True)
        self.service = CommandService(self.ledger, self.artifacts, providers=providers)
        opened = self.service.execute(
            {
                "protocolVersion": 1,
                "requestId": "request_open_synthetic",
                "operation": "task.open",
                "arguments": {
                    "goal": "Verify a synthetic fresh frame.",
                    "sessionId": "session_synthetic",
                    "target": "Synthetic/Target",
                    "allowedImpact": {
                        "hotfix": False,
                        "rebuildViews": [],
                        "reloadModules": [],
                        "restartPlayer": False,
                        "buildBaseline": False,
                    },
                    "acceptance": ["The fresh frame is registered by the Host."],
                },
            }
        )
        self.task_id = opened["result"]["taskId"]

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def command(self, request_id: str, **overrides: Any) -> dict[str, Any]:
        arguments = {
            "taskId": self.task_id,
            "checkSetId": "checkset_synthetic",
            "requireFreshFrame": True,
            "expectedViewportGeneration": 4,
            "expectedOwnerGeneration": 3,
            "targetId": "target_synthetic",
            "frameArtifactId": "capture_synthetic",
            "minimumFrameExclusive": 10,
            "maximumWidth": 64,
            "maximumHeight": 48,
        }
        arguments.update(overrides)
        return self.service.execute(
            {
                "protocolVersion": 1,
                "requestId": request_id,
                "operation": "verify",
                "taskId": self.task_id,
                "context": {
                    "sessionId": "session_synthetic",
                    "expectedRuntimeRevision": "revision_synthetic",
                    "expectedLaunchId": "launch_synthetic",
                },
                "arguments": arguments,
            }
        )

    def test_fresh_frame_is_registered_and_source_id_is_not_trusted(self) -> None:
        response = self.command("request_verify_fresh")
        self.assertEqual("completed", response["status"])
        self.assertEqual(1, len(response["artifacts"]))
        registered = response["artifacts"][0]
        self.assertNotEqual("capture_synthetic", registered["artifactId"])
        metadata, stream = self.artifacts.open_verified(registered["artifactId"])
        stream.close()
        self.assertEqual(registered, metadata)
        self.assertEqual(registered["artifactId"], response["result"]["freshFrame"]["artifactId"])
        self.assertEqual(registered["artifactId"], response["result"]["evidence"][0]["artifactId"])
        self.assertEqual(registered["sha256"], response["result"]["freshFrame"]["sha256"])

    def test_fresh_frame_path_must_be_inside_configured_artifact_root(self) -> None:
        outside = self.root / "outside.png"
        outside.write_bytes(b"synthetic outside frame")
        self.provider.frame_path = outside
        response = self.command("request_verify_outside_root")
        self.assertEqual("AUTH_REQUIRED", response["error"]["code"])
        self.assertEqual(0, self.ledger.summary()["counts"]["artifacts"])

    def test_fresh_frame_hash_mismatch_is_rejected(self) -> None:
        self.provider.tamper_hash = True
        response = self.command("request_verify_bad_hash")
        self.assertEqual("INPUT_CHANGED", response["error"]["code"])
        self.assertEqual(0, self.ledger.summary()["counts"]["artifacts"])

    def test_fresh_frame_binding_mismatch_is_rejected_before_registration(self) -> None:
        self.provider.owner_generation = 4
        response = self.command("request_verify_stale_owner")
        self.assertEqual("STALE_TARGET", response["error"]["code"])
        self.assertEqual(0, self.ledger.summary()["counts"]["artifacts"])

    def test_fresh_fact_must_reference_the_registered_frame_and_check_set(self) -> None:
        self.provider.claim_fresh_fact = True
        response = self.command("request_verify_fresh_fact")
        self.assertEqual("completed", response["status"])
        self.assertTrue(response["facts"]["freshVerified"])
        self.assertEqual(
            [response["artifacts"][0]["artifactId"]],
            response["result"]["freshVerification"]["evidenceArtifactIds"],
        )

    def test_verify_fields_remain_strict_and_check_set_is_preserved(self) -> None:
        normalized = validate_command(
            {
                "protocolVersion": 1,
                "requestId": "request_validate_fresh",
                "operation": "verify",
                "taskId": self.task_id,
                "arguments": {
                    "checkSetId": "checkset_synthetic",
                    "requireFreshFrame": True,
                    "expectedViewportGeneration": 4,
                    "expectedOwnerGeneration": 3,
                    "targetId": "target_synthetic",
                    "frameArtifactId": "capture_synthetic",
                    "minimumFrameExclusive": 10,
                    "maximumWidth": 64,
                    "maximumHeight": 48,
                },
            }
        )
        self.assertEqual("checkset_synthetic", normalized["arguments"]["checkSetId"])
        with self.assertRaises(Exception):
            validate_command(
                {
                    "protocolVersion": 1,
                    "requestId": "request_validate_unknown",
                    "operation": "verify",
                    "taskId": self.task_id,
                    "arguments": {
                        "checkSetId": "checkset_synthetic",
                        "requireFreshFrame": True,
                        "expectedViewportGeneration": 4,
                        "expectedOwnerGeneration": 3,
                        "targetId": "target_synthetic",
                        "frameArtifactId": "capture_synthetic",
                        "minimumFrameExclusive": 10,
                        "maximumWidth": 64,
                        "maximumHeight": 48,
                        "unknownField": True,
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
