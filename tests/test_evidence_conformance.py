from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from api.http_server import create_http_server
from clients.http_client import RelayHTTPClient, RelayHTTPError
from host.artifacts import ArtifactStore
from host.coordinator import UpdateCoordinator
from host.errors import CommandError
from host.ledger import Ledger
from host.providers import ProviderRegistry
from host.service import CommandService


def impact() -> dict[str, Any]:
    return {
        "hotfix": False,
        "rebuildViews": [],
        "reloadModules": [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


class SyntheticEvidenceProvider:
    def __init__(self) -> None:
        self.outcome: dict[str, Any] = {
            "status": "completed",
            "result": {},
            "runtimeChanged": False,
            "facts": {},
            "artifacts": [],
            "timingsMs": {},
        }
        self.calls = 0

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return copy.deepcopy(self.outcome)


class EvidenceConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.ledger = Ledger(self.root / "host.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])
        self.provider = SyntheticEvidenceProvider()
        providers = ProviderRegistry()
        providers.register("verification", "synthetic-evidence", self.provider, verified=True)
        self.service = CommandService(self.ledger, self.artifacts, providers=providers)
        self.task_id = self._open_task("session_evidence")

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def command(
        self,
        operation: str,
        arguments: dict[str, Any],
        request_id: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        command: dict[str, Any] = {
            "protocolVersion": 1,
            "requestId": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        effective_task_id = task_id if task_id is not None else arguments.get("taskId")
        if effective_task_id is not None:
            command["taskId"] = effective_task_id
        return self.service.execute(command)

    def _open_task(self, session_id: str) -> str:
        response = self.command(
            "task.open",
            {
                "goal": "Verify a synthetic state.",
                "sessionId": session_id,
                "target": "Synthetic/Target",
                "allowedImpact": impact(),
                "acceptance": ["Synthetic assertion passes."],
            },
            f"request_open_{session_id}",
        )
        self.assertEqual("completed", response["status"])
        return response["result"]["taskId"]

    def _image_artifact(self, task_id: str | None = None) -> dict[str, Any]:
        path = self.artifact_root / f"evidence_{len(list(self.artifact_root.iterdir()))}.png"
        path.write_bytes(b"synthetic image evidence")
        return self.artifacts.register(
            path,
            kind="screenshot",
            media_type="image/png",
            task_id=self.task_id if task_id is None else task_id,
        )

    def _valid_verified_outcome(self, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "completed",
            "result": {
                "checks": [
                    {
                        "checkId": "synthetic_check",
                        "status": "passed",
                        "evidenceArtifactIds": [artifact["artifactId"]],
                    }
                ],
                "visualReview": {
                    "reviewer": "synthetic-reviewer",
                    "artifactId": artifact["artifactId"],
                    "conclusion": "Synthetic target is visible.",
                    "reviewedAt": "2026-09-14T00:00:00Z",
                },
                "freshVerification": {
                    "sessionId": "session_evidence",
                    "baselineId": "baseline_synthetic",
                    "checkSetId": "checkset_synthetic",
                    "evidenceArtifactIds": [artifact["artifactId"]],
                },
            },
            "runtimeChanged": False,
            "facts": {
                "checksPassed": True,
                "visualReviewed": True,
                "freshVerified": True,
            },
            "artifacts": [artifact],
            "timingsMs": {"capture": 2.5, "assert": 1},
        }

    def test_verified_facts_are_atomic_durable_and_idempotent(self) -> None:
        artifact = self._image_artifact()
        self.provider.outcome = self._valid_verified_outcome(artifact)
        command = {"taskId": self.task_id, "checkSetId": "checkset_synthetic"}
        task_generation = self.ledger.get_task(self.task_id)["updatedAt"]
        response = self.command("verify", command, "request_verify_valid")

        self.assertEqual("completed", response["status"])
        self.assertEqual(
            {
                "sourceSaved": None,
                "runtimeMatched": None,
                "checksPassed": True,
                "visualReviewed": True,
                "freshVerified": True,
            },
            response["facts"],
        )
        records = self.service.result_policy.evidence.list_task(self.task_id)
        self.assertEqual(3, len(records))
        self.assertEqual({"checksPassed", "visualReviewed", "freshVerified"}, {item["fact"] for item in records})
        self.assertEqual(task_generation, self.ledger.get_task(self.task_id)["updatedAt"])

        replay = self.command("verify", command, "request_verify_valid")
        self.assertEqual(response, replay)
        self.assertEqual(1, self.provider.calls)
        self.assertEqual(3, self.service.result_policy.evidence.count())

        report = self.command("report", {"taskId": self.task_id}, "request_report_evidence")
        self.assertEqual(records, report["result"]["evidence"])

    def test_visual_truth_without_review_record_is_rejected_without_fact_write(self) -> None:
        self.provider.outcome["facts"] = {"visualReviewed": True}
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "missing-review"},
            "request_missing_visual_review",
        )
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])
        self.assertIsNone(self.ledger.get_task(self.task_id)["facts"]["visualReviewed"])
        self.assertEqual(0, self.service.result_policy.evidence.count())

    def test_failed_check_cannot_support_checks_passed(self) -> None:
        self.provider.outcome.update(
            {
                "facts": {"checksPassed": True},
                "result": {
                    "checks": [{"checkId": "failed", "status": "failed", "evidenceArtifactIds": []}]
                },
            }
        )
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "failed-check"},
            "request_failed_check_claim",
        )
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])
        self.assertIsNone(self.ledger.get_task(self.task_id)["facts"]["checksPassed"])

    def test_foreign_task_artifact_is_not_accepted_as_evidence(self) -> None:
        other_task = self._open_task("session_other")
        artifact = self._image_artifact(other_task)
        self.provider.outcome = self._valid_verified_outcome(artifact)
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "foreign-artifact"},
            "request_foreign_artifact",
        )
        self.assertEqual("AUTH_REQUIRED", response["error"]["code"])
        self.assertEqual(0, self.service.result_policy.evidence.count())

    def test_changed_artifact_bytes_are_detected_before_fact_write(self) -> None:
        artifact = self._image_artifact()
        self.provider.outcome = self._valid_verified_outcome(artifact)
        next(self.artifact_root.glob("*.png")).write_bytes(b"changed after registration")
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "changed-artifact"},
            "request_changed_artifact",
        )
        self.assertEqual("INPUT_CHANGED", response["error"]["code"])
        self.assertEqual(0, self.service.result_policy.evidence.count())

    def test_provider_error_shape_and_terminal_status_are_checked(self) -> None:
        self.provider.outcome = {
            "status": "failed",
            "result": {},
            "runtimeChanged": False,
            "facts": {},
            "artifacts": [],
            "timingsMs": {},
            "error": {"code": "INTERNAL_ERROR", "message": "missing fields"},
        }
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "bad-error"},
            "request_bad_provider_error",
        )
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])

    def test_accepted_result_cannot_claim_terminal_facts(self) -> None:
        self.provider.outcome.update(
            {
                "status": "accepted",
                "jobId": "job_external",
                "facts": {"checksPassed": False},
            }
        )
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "premature-fact"},
            "request_premature_fact",
        )
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])
        self.assertIsNone(self.ledger.get_task(self.task_id)["facts"]["checksPassed"])

    def test_accepted_result_requires_matching_durable_host_job(self) -> None:
        self.provider.outcome.update({"status": "accepted", "jobId": "job_missing"})
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "missing-job"},
            "request_missing_provider_job",
        )
        self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])

        job = self.ledger.create_job(
            {
                "jobId": "job_provider_owned",
                "requestId": "request_durable_provider_job",
                "operation": "verify",
                "taskId": self.task_id,
                "state": "queued",
                "stage": "provider_queued",
                "runtimeChanged": False,
            }
        )
        self.provider.outcome["jobId"] = job["jobId"]
        accepted = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "durable-job"},
            "request_durable_provider_job",
        )
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual(job["jobId"], accepted["jobId"])

    def test_explicit_false_is_recorded_separately_from_unknown(self) -> None:
        self.provider.outcome["facts"] = {"checksPassed": False}
        response = self.command(
            "verify",
            {"taskId": self.task_id, "checkSetId": "explicit-failure"},
            "request_explicit_false",
        )
        self.assertFalse(response["facts"]["checksPassed"])
        self.assertIsNone(response["facts"]["visualReviewed"])
        records = self.service.result_policy.evidence.list_task(self.task_id)
        self.assertEqual(1, len(records))
        self.assertFalse(records[0]["outcome"])

    def test_http_artifact_fetch_is_authenticated_and_rechecks_hash(self) -> None:
        artifact = self._image_artifact()
        server = create_http_server(self.service, "synthetic-token", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/artifacts/{artifact['artifactId']}"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer synthetic-token"})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(b"synthetic image evidence", response.read())
                self.assertEqual(artifact["sha256"], response.headers["X-Artifact-Sha256"])

            next(self.artifact_root.glob("*.png")).write_bytes(b"tampered")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(409, caught.exception.code)
            error = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual("INPUT_CHANGED", error["error"]["code"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_artifact_media_type_rejects_header_control_characters(self) -> None:
        path = self.artifact_root / "unsafe.bin"
        path.write_bytes(b"synthetic")
        with self.assertRaises(CommandError) as caught:
            self.artifacts.register(
                path,
                kind="evidence",
                media_type="application/octet-stream\r\nX-Injected: true",
                task_id=self.task_id,
            )
        self.assertEqual("CONTRACT_MISMATCH", caught.exception.code)

    def test_http_precondition_errors_always_have_correlation_ids(self) -> None:
        server = create_http_server(self.service, "correct-token", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/status"
            with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                urllib.request.urlopen(url, timeout=3)
            error = json.loads(unauthenticated.exception.read().decode("utf-8"))
            self.assertRegex(error["requestId"], r"^http_[0-9a-f]{32}$")

            client = RelayHTTPClient(f"http://127.0.0.1:{server.server_port}", "wrong-token")
            with self.assertRaises(RelayHTTPError) as wrong_token:
                client.command(
                    {
                        "protocolVersion": 1,
                        "requestId": "request_wrong_token_correlation",
                        "operation": "status",
                        "arguments": {},
                    }
                )
            self.assertEqual(
                "request_wrong_token_correlation",
                wrong_token.exception.response["requestId"],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class SyntheticPreparationProvider:
    provider_id = "synthetic-preparation"

    def __init__(self, store: ArtifactStore, root: Path, runtime: "SyntheticRuntimeProvider") -> None:
        self.store = store
        self.root = root
        self.runtime = runtime

    def current_input_snapshot(self, task: dict[str, Any]) -> str:
        return "snapshot_evidence"

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        path = self.root / f"prepared_{task['taskId']}.bin"
        path.write_bytes(b"synthetic prepared input")
        state = self.runtime.observe_state(task["sessionId"])
        return {
            "inputSnapshot": self.current_input_snapshot(task),
            "expectedRuntimeRevision": state["runtimeRevision"],
            "expectedRuntimeRevisionAfter": "runtime_2",
            "route": "HOTFIX",
            "requiredImpact": impact(),
            "artifacts": [self.store.register(path, kind="managed_dll", task_id=task["taskId"])],
            "requiredArtifactKinds": ["managed_dll"],
            "affectedModules": [],
            "affectedViews": [],
            "targetGenerations": {
                "moduleGeneration": state["moduleGeneration"],
                "resourceRelease": state["resourceRelease"],
                "viewGeneration": state["viewGeneration"],
            },
            "approvalRequired": False,
            "prepareComplete": True,
        }

    def reconcile_prepare(self, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
        return None


class SyntheticRuntimeProvider:
    provider_id = "synthetic-runtime"

    def __init__(self) -> None:
        self.state = {
            "sessionId": "session_apply_evidence",
            "runtimeRevision": "runtime_1",
            "moduleGeneration": 0,
            "resourceRelease": "resource_1",
            "viewGeneration": 0,
        }
        self.apply_calls = 0
        self.applied_plan_id: str | None = None
        self.claim_test_fact = False

    def observe_state(self, session_id: str) -> dict[str, Any]:
        return dict(self.state)

    def apply(self, plan: dict[str, Any], job_id: str) -> dict[str, Any]:
        self.apply_calls += 1
        self.applied_plan_id = plan["planId"]
        self.state["runtimeRevision"] = "runtime_2"
        return self.outcome()

    def reconcile(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
        return self.outcome() if self.applied_plan_id == plan["planId"] else None

    def outcome(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "runtimeChanged": True,
            "runtimeRevisionAfter": "runtime_2",
            "facts": {
                "sourceSaved": True,
                "runtimeMatched": True,
                "checksPassed": True if self.claim_test_fact else None,
            },
            "appliedSteps": ["synthetic_apply"],
            "methodStates": [],
            "error": None,
        }


class CoordinatorEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.ledger = Ledger(self.root / "host.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])
        self.runtime = SyntheticRuntimeProvider()
        self.preparation = SyntheticPreparationProvider(self.artifacts, self.artifact_root, self.runtime)
        self.coordinator = UpdateCoordinator(
            self.ledger,
            self.artifacts,
            self.preparation,
            self.runtime,
            preparation_verified=True,
            runtime_verified=True,
        )
        self.service = CommandService(self.ledger, self.artifacts, coordinator=self.coordinator)
        opened = self.command(
            "task.open",
            {
                "goal": "Apply a synthetic prepared plan.",
                "sessionId": "session_apply_evidence",
                "target": "Synthetic/ApplyTarget",
                "allowedImpact": impact(),
                "acceptance": ["Runtime revision matches."],
            },
            "request_open_apply_evidence",
        )
        self.task_id = opened["result"]["taskId"]

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def command(self, operation: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
        command: dict[str, Any] = {
            "protocolVersion": 1,
            "requestId": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        if "taskId" in arguments:
            command["taskId"] = arguments["taskId"]
        return self.service.execute(command)

    def wait_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.ledger.get_job(job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail(f"job did not finish: {job_id}")

    def prepare(self, request_id: str) -> dict[str, Any]:
        accepted = self.command("prepare", {"taskId": self.task_id}, request_id)
        self.assertEqual("accepted", accepted["status"])
        prepared = self.wait_job(accepted["jobId"])
        self.assertEqual("completed", prepared["state"])
        return prepared["result"]["plan"]

    def test_iterate_persists_source_and_runtime_evidence(self) -> None:
        plan = self.prepare("request_prepare_apply_evidence")
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_apply_evidence",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("completed", job["state"])
        self.assertTrue(job["result"]["facts"]["sourceSaved"])
        self.assertTrue(job["result"]["facts"]["runtimeMatched"])
        records = self.coordinator.evidence.list_task(self.task_id)
        self.assertEqual({"sourceSaved", "runtimeMatched"}, {item["fact"] for item in records})

    def test_restart_reconciliation_records_evidence_without_reapply(self) -> None:
        plan = self.prepare("request_prepare_recovery_evidence")
        interrupted = self.ledger.create_job(
            {
                "requestId": "request_interrupted_evidence",
                "operation": "iterate",
                "taskId": self.task_id,
                "planId": plan["planId"],
                "state": "running",
                "stage": "runtime_apply",
                "runtimeChanged": None,
            }
        )
        self.runtime.applied_plan_id = plan["planId"]
        self.runtime.state["runtimeRevision"] = "runtime_2"
        recovered = self.coordinator.recover_pending()
        terminal = next(item for item in recovered if item["jobId"] == interrupted["jobId"])
        self.assertEqual("completed", terminal["state"])
        self.assertEqual(0, self.runtime.apply_calls)
        self.assertEqual(2, len(self.coordinator.evidence.list_task(self.task_id)))

    def test_malformed_apply_result_reports_observed_runtime_change(self) -> None:
        plan = self.prepare("request_prepare_malformed_apply")
        self.runtime.claim_test_fact = True
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_malformed_apply",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("failed", job["state"])
        self.assertEqual("CONTRACT_MISMATCH", job["error"]["code"])
        self.assertTrue(job["runtimeChanged"])
        self.assertEqual(0, len(self.coordinator.evidence.list_task(self.task_id)))


if __name__ == "__main__":
    unittest.main()
