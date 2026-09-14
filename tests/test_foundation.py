from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from api.http_server import create_http_server
from clients.http_client import RelayHTTPClient, RelayHTTPError
from host.artifacts import ArtifactStore
from host.errors import CommandError
from host.ledger import Ledger
from host.service import CommandService
from host.validation import canonical_command_hash, validate_command


def allowed_impact() -> dict[str, object]:
    return {
        "hotfix": True,
        "rebuildViews": ["SyntheticView"],
        "reloadModules": [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.ledger = Ledger(self.root / "host.sqlite3")
        self.store = ArtifactStore(self.ledger, [self.artifact_root])
        self.service = CommandService(self.ledger, self.store)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def envelope(
        self,
        operation: str,
        arguments: dict[str, object],
        request_id: str,
        task_id: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "protocolVersion": 1,
            "requestId": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        if task_id:
            result["taskId"] = task_id
        return result

    def open_task(self, request_id: str = "request_open") -> tuple[dict[str, object], dict[str, object]]:
        command = self.envelope(
            "task.open",
            {
                "goal": "Adjust a synthetic panel.",
                "sessionId": "session_synthetic",
                "target": "SyntheticPanel/Action",
                "allowedImpact": allowed_impact(),
                "acceptance": ["The synthetic bound is satisfied."],
                "reference": None,
            },
            request_id,
        )
        return command, self.service.execute(command)

    def test_task_open_is_idempotent_and_unknown_facts_remain_null(self) -> None:
        command, first = self.open_task()
        second = self.service.execute(command)
        self.assertEqual(first, second)
        self.assertEqual("completed", first["status"])
        self.assertTrue(all(value is None for value in first["facts"].values()))
        self.assertEqual(1, self.ledger.summary()["counts"]["tasks"])

    def test_same_request_id_with_different_payload_is_rejected(self) -> None:
        _command, first = self.open_task("request_collision")
        collision = self.service.execute(self.envelope("status", {}, "request_collision"))
        self.assertEqual("completed", first["status"])
        self.assertEqual("failed", collision["status"])
        self.assertEqual("CONTRACT_MISMATCH", collision["error"]["code"])

    def test_orphaned_request_requires_reconciliation(self) -> None:
        command = self.envelope("status", {}, "request_orphaned")
        normalized = validate_command(command)
        self.ledger.begin_command(
            "request_orphaned",
            canonical_command_hash(normalized),
            "status",
            None,
        )
        response = self.service.execute(command)
        self.assertEqual("state_unknown", response["status"])
        self.assertEqual("STATE_UNKNOWN", response["error"]["code"])
        self.assertIsNone(response["runtimeChanged"])

    def test_unavailable_provider_never_returns_empty_success(self) -> None:
        _command, opened = self.open_task()
        task_id = opened["result"]["taskId"]
        response = self.service.execute(
            self.envelope("observe", {"taskId": task_id}, "request_observe", task_id)
        )
        self.assertEqual("failed", response["status"])
        self.assertEqual("CAPABILITY_UNAVAILABLE", response["error"]["code"])
        self.assertFalse(response["runtimeChanged"])

    def test_operation_arguments_are_validated_by_type_and_range(self) -> None:
        command = self.envelope(
            "task.open",
            {
                "goal": "Synthetic goal",
                "sessionId": "session_synthetic",
                "target": "SyntheticTarget",
                "allowedImpact": {**allowed_impact(), "hotfix": 1},
                "acceptance": [],
            },
            "request_bad_types",
        )
        response = self.service.execute(command)
        self.assertEqual("failed", response["status"])
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])
        self.assertEqual(0, self.ledger.summary()["counts"]["tasks"])

    def test_result_error_matches_public_v1_shape(self) -> None:
        response = self.service.execute({})
        self.assertEqual("invalid-request", response["requestId"])
        self.assertEqual(
            {"code", "stage", "message", "recoverable", "details"},
            set(response["error"]),
        )
        self.assertIsInstance(response["error"]["recoverable"], bool)

    def test_artifact_scope_hash_and_immutable_read(self) -> None:
        artifact_path = self.artifact_root / "synthetic.txt"
        artifact_path.write_bytes(b"synthetic evidence")
        expected = hashlib.sha256(b"synthetic evidence").hexdigest()
        metadata = self.store.register(artifact_path, kind="evidence", expected_sha256=expected)
        self.assertEqual(
            {"artifactId", "kind", "sha256", "mediaType", "size"},
            set(metadata),
        )
        read_metadata, stream = self.store.open_verified(metadata["artifactId"])
        try:
            self.assertEqual(b"synthetic evidence", stream.read())
            self.assertEqual(expected, read_metadata["sha256"])
        finally:
            stream.close()
        artifact_path.write_bytes(b"changed")
        with self.assertRaises(CommandError) as changed:
            self.store.open_verified(metadata["artifactId"])
        self.assertEqual("INPUT_CHANGED", changed.exception.code)

    def test_artifact_outside_configured_roots_is_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(CommandError) as rejected:
            self.store.register(outside, kind="evidence")
        self.assertEqual("AUTH_REQUIRED", rejected.exception.code)

    def test_method_ledger_detects_source_reversion_after_patch(self) -> None:
        self.ledger.set_method_state(
            {
                "sessionId": "session_synthetic",
                "assemblyId": "assembly_synthetic",
                "moduleGeneration": 0,
                "methodId": "method_compute",
                "sourceHash": "a" * 64,
                "appliedHash": "a" * 64,
                "state": "matched",
            }
        )
        self.assertEqual("b" * 64, self.ledger.method_delta(
            "session_synthetic", "assembly_synthetic", 0, {"method_compute": "b" * 64}
        )[0]["sourceHash"])
        self.ledger.set_method_state(
            {
                "sessionId": "session_synthetic",
                "assemblyId": "assembly_synthetic",
                "moduleGeneration": 0,
                "methodId": "method_compute",
                "sourceHash": "b" * 64,
                "appliedHash": "b" * 64,
                "state": "matched",
            }
        )
        restore = self.ledger.method_delta(
            "session_synthetic", "assembly_synthetic", 0, {"method_compute": "a" * 64}
        )
        self.assertEqual("restore_or_update", restore[0]["change"])
        self.assertEqual("a" * 64, restore[0]["sourceHash"])

    def test_plan_specific_confirmation_can_expand_task_impact(self) -> None:
        _command, opened = self.open_task()
        task_id = opened["result"]["taskId"]
        plan = self.ledger.create_plan(
            {
                "taskId": task_id,
                "sessionId": "session_synthetic",
                "inputSnapshot": "snapshot_synthetic",
                "expectedRuntimeRevision": "runtime_synthetic",
                "route": "HOTFIX",
                "state": "prepared",
                "prepareComplete": True,
                "approvalRequired": True,
                "details": {"requiredImpact": allowed_impact()},
            }
        )
        expanded = {**allowed_impact(), "restartPlayer": True}
        response = self.service.execute(
            self.envelope(
                "task.approve",
                {
                    "taskId": task_id,
                    "planId": plan["planId"],
                    "userConfirmationRef": "confirmation_synthetic",
                    "approvedImpact": expanded,
                },
                "request_expanded_approval",
                task_id,
            )
        )
        self.assertEqual("completed", response["status"])
        self.assertEqual(plan["planId"], response["result"]["approval"]["planId"])
        self.assertEqual(expanded, response["result"]["approval"]["approvedImpact"])
        self.assertEqual("confirmation_synthetic", response["result"]["approval"]["userConfirmationRef"])


class HTTPAndCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        artifact_root = root / "artifacts"
        artifact_root.mkdir()
        self.ledger = Ledger(root / "host.sqlite3")
        store = ArtifactStore(self.ledger, [artifact_root])
        self.service = CommandService(self.ledger, store)
        self.token = "synthetic-local-token"
        self.server = create_http_server(self.service, self.token, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.ledger.close()
        self.temporary.cleanup()

    def test_actual_http_status_and_authentication(self) -> None:
        client = RelayHTTPClient(self.base_url, self.token)
        status = client.status()
        self.assertEqual("Relay LiveLoop", status["service"])
        self.assertTrue(all(not item["available"] for item in client.capabilities()["capabilities"]))
        with self.assertRaises(RelayHTTPError) as unauthorized:
            RelayHTTPClient(self.base_url, "wrong-token").status()
        self.assertEqual(401, unauthorized.exception.status)

    def test_actual_cli_maps_to_same_http_command_service(self) -> None:
        script = Path(__file__).resolve().parents[1] / "relay_liveloop.py"
        environment = os.environ.copy()
        environment["RELAY_LIVELOOP_TOKEN"] = self.token
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "status",
                "--url",
                self.base_url,
                "--request-id",
                "request_cli_status",
                "--json",
            ],
            cwd=script.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertEqual("completed", response["status"])
        self.assertEqual("Relay LiveLoop", response["result"]["service"])
        replay = RelayHTTPClient(self.base_url, self.token).command(
            {
                "protocolVersion": 1,
                "requestId": "request_cli_status",
                "operation": "status",
                "arguments": {},
            }
        )
        self.assertEqual(response, replay)


if __name__ == "__main__":
    unittest.main()
