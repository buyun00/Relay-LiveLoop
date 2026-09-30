from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from host.artifacts import ArtifactStore
from host.editor_transport import EditorJobTransport
from host.ledger import Ledger
from host.source_save import EditorSourceProvider
from relay_liveloop import build_command_service
from tests.test_recovery_consistency_runtime_attribution import synthetic_impact


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class Resolver:
    is_verified = True
    source_save_contract_version = 1

    def __init__(self, root):
        self.root = root
        self.launch = "launch-synthetic"
        self.page = "page-synthetic"
        self.generation = 1
        self.path = "Assets/Synthetic/Widget.prefab"

    def resolve(self, task, source_id, context):
        return {"taskId": task["taskId"], "taskUpdatedAt": task["updatedAt"], "sessionId": task["sessionId"],
                "launchId": self.launch, "runtimeRevision": "revision-synthetic", "targetId": task["target"],
                "pageId": self.page, "ownerGeneration": self.generation, "sourceId": "source-synthetic", "sourceGuid": "a" * 32,
                "localId": 42, "assetPath": self.path, "sourceHash": hashlib.sha256((self.root / self.path).read_bytes()).hexdigest(),
                "properties": ["position.x"]}

    def validate(self, task, binding):
        from host.errors import CommandError
        if binding["launchId"] != self.launch or binding["pageId"] != self.page or binding["ownerGeneration"] != self.generation:
            raise CommandError("STALE_TARGET", "Synthetic runtime binding changed.", stage="source_save", runtime_changed=False)


class SourceSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-source-save-")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.asset = self.workspace / "Assets/Synthetic/Widget.prefab"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b"synthetic-before")
        Path(str(self.asset) + ".meta").write_text("guid: " + "a" * 32 + "\n")
        (self.root / "artifacts").mkdir()
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.root / "artifacts"])
        self.transport = EditorJobTransport(self.root / "mailbox", self.root / "artifacts")
        self.resolver = Resolver(self.workspace)
        self.provider = EditorSourceProvider(self.ledger, self.artifacts, self.transport, self.resolver, self.workspace, "editor-synthetic")
        self.service = build_command_service(self.ledger, self.artifacts, source_provider=self.provider)
        self.task = self.ledger.create_task({"sessionId": "session-synthetic", "goal": "Save one synthetic asset.", "target": "target-synthetic",
                                             "allowedImpact": synthetic_impact(), "acceptance": ["Saved and applied facts are separate."]})
        self.sequence = 0

    def tearDown(self):
        self.service.close(); self.ledger.close(); self.tmp.cleanup()

    def command(self, operation, arguments, request_id=None):
        self.sequence += 1
        return {"protocolVersion": 1, "requestId": request_id or f"request-source-{self.sequence}", "operation": operation,
                "taskId": self.task["taskId"], "context": {"sessionId": "session-synthetic", "expectedLaunchId": "launch-synthetic",
                                                           "expectedRuntimeRevision": "revision-synthetic"}, "arguments": arguments}

    def start(self):
        args = {"expectedSourceHash": hashlib.sha256(self.asset.read_bytes()).hexdigest(),
                "edits": [{"sourceId": "source-synthetic", "property": "position.x", "expectedValue": 1, "newValue": 2}]}
        command = self.command("source.edit", args)
        response = self.service.execute(command)
        self.assertEqual("accepted", response["status"], response)
        self.assertIsNone(response["facts"]["sourceSaved"])
        return response["jobId"], command

    def publish(self, job_id, *, corrupt_binding=False, phase="completed", artifact=True):
        job = self.ledger.get_job(job_id); record = job["result"]
        self.asset.write_bytes(b"synthetic-after")
        after_hash = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        binding = deepcopy(record["binding"])
        if corrupt_binding:
            binding["launchId"] = "other-launch"
        receipt = {"schema": "relay.liveloop.source-save-receipt", "version": 1, "jobId": job_id,
                   "attemptId": "attempt-synthetic", "requestDigest": record["requestDigest"], "bindingJson": canonical(binding),
                   "phase": phase, "beforeHash": record["binding"]["sourceHash"], "afterHash": after_hash,
                   "sourcePersisted": phase == "completed", "sourceChangedKnown": phase != "state_unknown", "sourceChanged": phase == "completed",
                   "runtimeApplied": False, "rollbackAttempted": False, "rollbackSucceeded": False, "errorCode": "", "errorMessage": ""}
        receipt_path = self.root / "artifacts" / "attempt-synthetic" / "source-save-receipt.json"
        receipt_path.parent.mkdir(); receipt_path.write_text(canonical(receipt))
        raw = {"jobId": job_id, "requestDigest": record["requestDigest"], "inputSnapshot": record["inputSnapshot"], "providerId": "editor-synthetic",
               "attemptId": "attempt-synthetic", "status": "completed", "completedAtUtc": "2026-01-01T00:00:00Z", "resultJson": canonical(receipt), "error": None,
               "artifacts": [{"artifactId": "source-receipt-synthetic", "kind": "source_save_receipt", "path": str(receipt_path),
                              "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(), "mediaType": "application/json", "size": receipt_path.stat().st_size}] if artifact else []}
        result = self.root / "mailbox/results" / f"{job_id}.result.json"
        result.write_text(canonical(raw))
        # The worker's normal terminal path archives the incoming request.
        (self.root / "mailbox/incoming" / f"{job_id}.request.json").unlink()

    def status(self, job_id):
        return self.service.execute(self.command("job.status", {"jobId": job_id}))["result"]["job"]

    def test_locate_proves_guid_hash_and_current_runtime_source_binding(self):
        response = self.service.execute(self.command("source.locate", {"target": "target-synthetic"}))
        self.assertEqual("completed", response["status"])
        self.assertEqual("a" * 32, response["result"]["source"]["sourceGuid"])
        self.assertIsNone(response["facts"]["sourceSaved"])

    def test_saved_receipt_survives_host_restart_without_runtime_claim(self):
        job_id, command = self.start(); self.publish(job_id)
        terminal = self.status(job_id)
        self.assertEqual("completed", terminal["state"], terminal)
        self.assertTrue(terminal["result"]["sourceSaved"])
        self.assertTrue(terminal["result"]["sourceChanged"])
        self.assertFalse(terminal["result"]["runtimeApplied"])
        task = self.ledger.get_task(self.task["taskId"])
        self.assertTrue(task["facts"]["sourceSaved"])
        self.assertIsNone(task["facts"]["runtimeMatched"])
        reopened = Ledger(self.root / "ledger.sqlite3")
        try:
            self.assertEqual(terminal, reopened.get_job(job_id))
        finally:
            reopened.close()
        self.assertEqual(job_id, self.service.execute(command)["jobId"])

    def test_lost_dispatch_has_no_second_submission_on_recovery(self):
        job_id, _ = self.start()
        (self.root / "mailbox/incoming" / f"{job_id}.request.json").unlink()
        self.provider.recover_pending()
        terminal = self.ledger.get_job(job_id)
        self.assertEqual("state_unknown", terminal["state"])
        self.assertFalse((self.root / "mailbox/incoming" / f"{job_id}.request.json").exists())
        self.assertIsNone(self.ledger.get_task(self.task["taskId"])["facts"]["sourceSaved"])

    def test_save_before_host_dispatch_progress_recovers_existing_result(self):
        job_id, _ = self.start(); self.publish(job_id)
        job = self.ledger.get_job(job_id)
        self.ledger.update_job(job_id, state="queued", stage="source_queued", runtime_changed=False, result=job["result"])
        self.provider.recover_pending()
        self.assertEqual("completed", self.ledger.get_job(job_id)["state"])
        self.assertTrue(self.ledger.get_task(self.task["taskId"])["facts"]["sourceSaved"])
        self.assertFalse((self.root / "mailbox/incoming" / f"{job_id}.request.json").exists())

    def test_next_accepted_save_does_not_project_previous_saved_fact(self):
        job_id, _ = self.start(); self.publish(job_id); self.status(job_id)
        next_job_id, _ = self.start()
        self.assertNotEqual(job_id, next_job_id)

    def test_saved_source_invalidates_prior_runtime_check_and_review_facts(self):
        self.ledger.update_task_facts(self.task["taskId"], {"runtimeMatched": True, "checksPassed": True,
            "visualReviewed": True, "freshVerified": True})
        job_id, _ = self.start(); self.publish(job_id); self.status(job_id)
        facts = self.ledger.get_task(self.task["taskId"])["facts"]
        self.assertTrue(facts["sourceSaved"])
        for key in ("runtimeMatched", "checksPassed", "visualReviewed", "freshVerified"):
            self.assertIsNone(facts[key])

    def test_stale_receipt_wrong_launch_is_rejected(self):
        job_id, _ = self.start(); self.publish(job_id, corrupt_binding=True)
        terminal = self.status(job_id)
        self.assertEqual("failed", terminal["state"])
        self.assertEqual("STALE_TARGET", terminal["error"]["code"])
        self.assertIsNone(self.ledger.get_task(self.task["taskId"])["facts"]["sourceSaved"])

    def test_page_or_generation_change_rejects_delayed_result(self):
        job_id, _ = self.start(); self.publish(job_id)
        self.resolver.generation += 1
        self.assertEqual("failed", self.status(job_id)["state"])
        self.assertIsNone(self.ledger.get_task(self.task["taskId"])["facts"]["sourceSaved"])

    def test_saved_result_without_sealed_receipt_cannot_write_saved_fact(self):
        job_id, _ = self.start(); self.publish(job_id, artifact=False)
        self.assertEqual("CONTRACT_MISMATCH", self.status(job_id)["error"]["code"])
        self.assertIsNone(self.ledger.get_task(self.task["taskId"])["facts"]["sourceSaved"])

    def test_dispatched_source_save_is_not_cancellable(self):
        job_id, _ = self.start()
        response = self.service.execute(self.command("job.cancel", {"jobId": job_id}))
        self.assertFalse(response["result"]["cancelAccepted"])

    def test_scene_source_and_wrong_guid_are_refused(self):
        Path(str(self.asset) + ".meta").write_text("guid: " + "b" * 32 + "\n")
        response = self.service.execute(self.command("source.locate", {"target": "target-synthetic"}))
        self.assertEqual("INPUT_CHANGED", response["error"]["code"])
        scene = self.workspace / "Assets/Synthetic/Widget.unity"; scene.write_bytes(b"synthetic-scene")
        self.resolver.path = "Assets/Synthetic/Widget.unity"
        response = self.service.execute(self.command("source.locate", {"target": "target-synthetic"}))
        self.assertEqual("AUTH_REQUIRED", response["error"]["code"])

    def test_v005_backend_without_transaction_contract_is_unavailable(self):
        self.resolver.source_save_contract_version = None
        response = self.service.execute(self.command("source.locate", {"target": "target-synthetic"}))
        self.assertEqual("CAPABILITY_UNAVAILABLE", response["error"]["code"])

    def test_wrong_source_id_cannot_be_silently_rebound(self):
        response = self.service.execute(self.command("source.edit", {
            "expectedSourceHash": hashlib.sha256(self.asset.read_bytes()).hexdigest(), "edits": [
                {"sourceId": "other-source", "property": "position.x", "expectedValue": 1, "newValue": 2}]}))
        self.assertEqual("STALE_TARGET", response["error"]["code"])

    def test_http_cli_mcp_use_the_same_source_save_job_and_ledger(self):
        from api.http_server import create_http_server
        from clients.http_client import RelayHTTPClient
        from mcp.stdio_server import MCPServer, ToolCatalog
        from tests.test_mcp_stdio import modern_meta
        token = "synthetic-source-transport-token"
        server = create_http_server(self.service, token, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            client = RelayHTTPClient(url, token)
            located = client.command(self.command("source.locate", {"target": "target-synthetic"}))
            self.assertEqual("completed", located["status"])
            command = self.command("source.edit", {"expectedSourceHash": located["result"]["source"]["sourceHash"],
                "edits": [{"sourceId": "source-synthetic", "property": "position.x", "expectedValue": 1, "newValue": 2}]})
            environment = os.environ.copy(); environment["RELAY_LIVELOOP_TOKEN"] = token
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, str(root / "relay_liveloop.py"), "command", "source.edit",
                "--url", url, "--task", self.task["taskId"], "--request-id", command["requestId"],
                "--arguments", canonical(command["arguments"]), "--context", canonical(command["context"]), "--json"],
                env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            accepted = json.loads(result.stdout); self.assertEqual("accepted", accepted["status"])
            self.assertIsNone(accepted["facts"]["sourceSaved"])
            self.publish(accepted["jobId"])
            catalog = ToolCatalog.load(root / "contracts/operations.json", root / "contracts/result.schema.json")
            mcp = MCPServer(client, catalog)
            polled = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "_meta": modern_meta(), "name": "relay_liveloop_job_status", "arguments": {
                    "requestId": "request-source-mcp-poll", "jobId": accepted["jobId"]}}})
            job = polled["result"]["structuredContent"]["result"]["job"]
            self.assertEqual(accepted["jobId"], job["jobId"])
            self.assertEqual("source_saved", job["stage"])
            self.assertTrue(job["result"]["sourceSaved"])
            self.assertFalse(job["result"]["runtimeApplied"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
