from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from host.artifacts import ArtifactStore
from host.coordinator import UpdateCoordinator
from host.ledger import Ledger
from host.service import CommandService


def impact(*, hotfix: bool = True, view: bool = True, restart: bool = False) -> dict[str, Any]:
    return {
        "hotfix": hotfix,
        "rebuildViews": ["SyntheticView"] if view else [],
        "reloadModules": [],
        "restartPlayer": restart,
        "buildBaseline": False,
    }


class SyntheticPreparationProvider:
    provider_id = "synthetic-preparation"

    def __init__(self, store: ArtifactStore, root: Path, runtime: "SyntheticRuntimeProvider"):
        self.store = store
        self.root = root
        self.runtime = runtime
        self.snapshot = "snapshot_1"
        self.required_impact = impact()
        self.omit_resource = False
        self.prepare_calls = 0

    def current_input_snapshot(self, task: dict[str, Any]) -> str:
        return self.snapshot

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.prepare_calls += 1
        managed = self.root / f"managed_{self.prepare_calls}.bin"
        managed.write_bytes(b"synthetic managed artifact")
        artifacts = [self.store.register(managed, kind="managed_dll")]
        if not self.omit_resource:
            resource = self.root / f"resource_{self.prepare_calls}.bin"
            resource.write_bytes(b"synthetic resource artifact")
            artifacts.append(self.store.register(resource, kind="resource_release"))
        state = self.runtime.observe_state(task["sessionId"])
        return {
            "inputSnapshot": self.snapshot,
            "expectedRuntimeRevision": state["runtimeRevision"],
            "expectedRuntimeRevisionAfter": "runtime_2",
            "route": "HOTFIX_AND_VIEW",
            "requiredImpact": self.required_impact,
            "artifacts": artifacts,
            "requiredArtifactKinds": ["managed_dll", "resource_release"],
            "affectedModules": [],
            "affectedViews": ["SyntheticView"],
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
            "sessionId": "session_synthetic",
            "runtimeRevision": "runtime_1",
            "moduleGeneration": 0,
            "resourceRelease": "resource_1",
            "viewGeneration": 0,
        }
        self.apply_calls = 0
        self.reconcile_calls = 0
        self.lose_response = False
        self.uncertain_reconcile = False
        self.applied_plan_id: str | None = None

    def observe_state(self, session_id: str) -> dict[str, Any]:
        return dict(self.state)

    def apply(self, plan: dict[str, Any], job_id: str) -> dict[str, Any]:
        self.apply_calls += 1
        self.state.update(
            {
                "runtimeRevision": "runtime_2",
                "resourceRelease": "resource_2",
                "viewGeneration": 1,
            }
        )
        self.applied_plan_id = plan["planId"]
        if self.lose_response:
            raise TimeoutError("synthetic lost response")
        return self._outcome()

    def reconcile(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
        self.reconcile_calls += 1
        if self.uncertain_reconcile or self.applied_plan_id != plan["planId"]:
            return None
        return self._outcome()

    @staticmethod
    def _outcome() -> dict[str, Any]:
        return {
            "status": "completed",
            "runtimeChanged": True,
            "runtimeRevisionAfter": "runtime_2",
            "facts": {
                "sourceSaved": True,
                "runtimeMatched": True,
                "checksPassed": None,
                "visualReviewed": None,
                "freshVerified": None,
            },
            "appliedSteps": ["synthetic_apply", "synthetic_reconcile"],
            "methodStates": [],
            "error": None,
        }


class CoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.ledger = Ledger(self.root / "host.sqlite3")
        self.store = ArtifactStore(self.ledger, [self.artifact_root])
        self.runtime = SyntheticRuntimeProvider()
        self.preparation = SyntheticPreparationProvider(self.store, self.artifact_root, self.runtime)
        self.coordinator = UpdateCoordinator(
            self.ledger,
            self.store,
            self.preparation,
            self.runtime,
            preparation_verified=True,
            runtime_verified=True,
        )
        self.service = CommandService(self.ledger, self.store, coordinator=self.coordinator)
        self.task_id = self._open_task()

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def command(self, operation: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "protocolVersion": 1,
            "requestId": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        if "taskId" in arguments:
            envelope["taskId"] = arguments["taskId"]
        return self.service.execute(envelope)

    def _open_task(self) -> str:
        response = self.command(
            "task.open",
            {
                "goal": "Exercise a synthetic update.",
                "sessionId": "session_synthetic",
                "target": "SyntheticView/Target",
                "allowedImpact": impact(),
                "acceptance": ["Synthetic state matches."],
            },
            "request_open_coordination",
        )
        return response["result"]["taskId"]

    def wait_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.ledger.get_job(job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail(f"job did not finish: {job_id}")

    def prepare(self, request_id: str = "request_prepare") -> tuple[dict[str, Any], dict[str, Any]]:
        accepted = self.command("prepare", {"taskId": self.task_id}, request_id)
        self.assertEqual("accepted", accepted["status"])
        return accepted, self.wait_job(accepted["jobId"])

    def test_prepare_is_build_only_and_creates_immutable_plan(self) -> None:
        before = dict(self.runtime.state)
        _accepted, job = self.prepare()
        self.assertEqual("completed", job["state"])
        self.assertEqual(before, self.runtime.state)
        self.assertEqual(0, self.runtime.apply_calls)
        plan = job["result"]["plan"]
        self.assertTrue(plan["prepareComplete"])
        self.assertEqual({"managed_dll", "resource_release"}, {item["kind"] for item in plan["details"]["artifacts"]})

    def test_joint_prepare_missing_one_artifact_never_applies_runtime(self) -> None:
        self.preparation.omit_resource = True
        before = dict(self.runtime.state)
        _accepted, job = self.prepare("request_prepare_incomplete")
        self.assertEqual("failed", job["state"])
        self.assertEqual("RESOURCE_BUILD_FAILED", job["error"]["code"])
        self.assertEqual(before, self.runtime.state)
        self.assertEqual(0, self.runtime.apply_calls)

    def test_stale_runtime_revision_rejects_before_apply(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_stale")
        plan = prepared["result"]["plan"]
        self.runtime.state["runtimeRevision"] = "runtime_external"
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_stale",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("failed", job["state"])
        self.assertEqual("STALE_TARGET", job["error"]["code"])
        self.assertEqual(0, self.runtime.apply_calls)

    def test_task_goal_generation_invalidates_prepared_plan(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_task_generation")
        plan = prepared["result"]["plan"]
        updated = self.command(
            "task.update",
            {"taskId": self.task_id, "updates": {"goal": "Changed synthetic goal."}},
            "request_task_update_generation",
        )
        self.assertEqual("completed", updated["status"])
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_old_task_generation",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("INPUT_CHANGED", job["error"]["code"])
        self.assertEqual(0, self.runtime.apply_calls)

    def test_source_input_snapshot_invalidates_prepared_plan(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_source_generation")
        plan = prepared["result"]["plan"]
        self.preparation.snapshot = "snapshot_2"
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_old_source_generation",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("INPUT_CHANGED", job["error"]["code"])
        self.assertEqual(0, self.runtime.apply_calls)

    def test_target_generation_change_rejects_even_when_revision_text_is_same(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_target_generation")
        plan = prepared["result"]["plan"]
        self.runtime.state["viewGeneration"] = 1
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_old_target_generation",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("STALE_TARGET", job["error"]["code"])
        self.assertEqual(0, self.runtime.apply_calls)

    def test_artifact_change_after_prepare_rejects_before_apply(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_artifact_change")
        plan = prepared["result"]["plan"]
        (self.artifact_root / "managed_1.bin").write_bytes(b"tampered synthetic artifact")
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_changed_artifact",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("INPUT_CHANGED", job["error"]["code"])
        self.assertEqual(0, self.runtime.apply_calls)

    def test_lost_apply_response_reconciles_without_second_apply(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_reconcile")
        plan = prepared["result"]["plan"]
        self.runtime.lose_response = True
        command = {
            "taskId": self.task_id,
            "planId": plan["planId"],
        }
        accepted = self.command("iterate", command, "request_iterate_reconcile")
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("completed", job["state"])
        self.assertEqual(1, self.runtime.apply_calls)
        self.assertEqual(1, self.runtime.reconcile_calls)
        replay = self.command("iterate", command, "request_iterate_reconcile")
        self.assertEqual(accepted, replay)
        self.assertEqual(1, self.runtime.apply_calls)

    def test_unknown_reconciliation_stays_state_unknown(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_unknown")
        plan = prepared["result"]["plan"]
        self.runtime.lose_response = True
        self.runtime.uncertain_reconcile = True
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_unknown",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("state_unknown", job["state"])
        self.assertEqual("STATE_UNKNOWN", job["error"]["code"])
        self.assertEqual(1, self.runtime.apply_calls)

    def test_restart_recovery_reconciles_dispatched_job_without_applying_again(self) -> None:
        _accepted, prepared = self.prepare("request_prepare_restart_recovery")
        plan = prepared["result"]["plan"]
        interrupted = self.ledger.create_job(
            {
                "requestId": "request_interrupted_runtime",
                "operation": "iterate",
                "taskId": self.task_id,
                "planId": plan["planId"],
                "state": "running",
                "stage": "runtime_apply",
                "runtimeChanged": None,
            }
        )
        self.runtime.state.update(
            {"runtimeRevision": "runtime_2", "resourceRelease": "resource_2", "viewGeneration": 1}
        )
        self.runtime.applied_plan_id = plan["planId"]
        recovered = self.coordinator.recover_pending()
        self.assertTrue(any(item["jobId"] == interrupted["jobId"] for item in recovered))
        terminal = self.ledger.get_job(interrupted["jobId"])
        self.assertEqual("completed", terminal["state"])
        self.assertEqual(0, self.runtime.apply_calls)
        self.assertEqual(1, self.runtime.reconcile_calls)

    def test_wrong_command_session_is_rejected_before_job_creation(self) -> None:
        before = self.ledger.summary()["counts"]["jobs"]
        response = self.service.execute(
            {
                "protocolVersion": 1,
                "requestId": "request_wrong_session",
                "operation": "prepare",
                "taskId": self.task_id,
                "context": {"sessionId": "session_other"},
                "arguments": {"taskId": self.task_id},
            }
        )
        self.assertEqual("WRONG_SESSION", response["error"]["code"])
        self.assertEqual(before, self.ledger.summary()["counts"]["jobs"])

    def test_plan_specific_user_confirmation_can_expand_initial_impact(self) -> None:
        self.preparation.required_impact = impact(restart=True)
        _accepted, prepared = self.prepare("request_prepare_approval")
        plan = prepared["result"]["plan"]
        self.assertTrue(plan["approvalRequired"])
        approval = self.command(
            "task.approve",
            {
                "taskId": self.task_id,
                "planId": plan["planId"],
                "userConfirmationRef": "synthetic_user_confirmation",
                "approvedImpact": impact(restart=True),
            },
            "request_approve_expanded_plan",
        )
        self.assertEqual("completed", approval["status"])
        accepted = self.command(
            "iterate",
            {"taskId": self.task_id, "planId": plan["planId"]},
            "request_iterate_approved",
        )
        job = self.wait_job(accepted["jobId"])
        self.assertEqual("completed", job["state"])
        self.assertEqual(1, self.runtime.apply_calls)

    def test_unverified_configured_provider_is_honestly_unavailable(self) -> None:
        unverified = UpdateCoordinator(self.ledger, self.store, self.preparation, self.runtime)
        unverified_service = CommandService(self.ledger, self.store, coordinator=unverified)
        try:
            response = unverified_service.execute(
                {
                    "protocolVersion": 1,
                    "requestId": "request_unverified_provider",
                    "operation": "prepare",
                    "taskId": self.task_id,
                    "arguments": {"taskId": self.task_id},
                }
            )
            self.assertEqual("CAPABILITY_UNAVAILABLE", response["error"]["code"])
        finally:
            unverified_service.close()


if __name__ == "__main__":
    unittest.main()
