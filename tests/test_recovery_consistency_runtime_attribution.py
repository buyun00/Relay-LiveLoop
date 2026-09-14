from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from host.artifacts import ArtifactStore
from host.coordinator import UpdateCoordinator
from host.evidence import EvidenceStore
from host.ledger import Ledger


def synthetic_impact() -> dict[str, Any]:
    return {
        "hotfix": True,
        "rebuildViews": [],
        "reloadModules": [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


class SavedSourceProvider:
    provider_id = "source-synthetic"

    @staticmethod
    def current_input_snapshot(task: dict[str, Any]) -> str:
        return "snapshot-synthetic"


class ReconciliationProvider:
    provider_id = "runtime-synthetic"

    def __init__(self, observed_revision: str, reported_runtime_changed: bool | None) -> None:
        self.apply_calls = 0
        self.observe_calls = 0
        self.reconcile_calls = 0
        self.reported_runtime_changed = reported_runtime_changed
        self.state = {
            "sessionId": "session-synthetic",
            "runtimeRevision": observed_revision,
            "moduleGeneration": 0,
            "resourceRelease": "release-synthetic",
            "viewGeneration": 0,
        }

    def observe_state(self, session_id: str) -> dict[str, Any]:
        self.observe_calls += 1
        return dict(self.state)

    def apply(self, plan: dict[str, Any], job_id: str) -> dict[str, Any]:
        self.apply_calls += 1
        raise AssertionError("Restart recovery must never replay an uncertain apply.")

    def reconcile(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        self.reconcile_calls += 1
        return {
            "status": "completed",
            "runtimeChanged": self.reported_runtime_changed,
            "runtimeRevisionAfter": self.state["runtimeRevision"],
            "facts": {
                "sourceSaved": True,
                "runtimeMatched": True,
                "checksPassed": None,
                "visualReviewed": None,
                "freshVerified": None,
            },
            "appliedSteps": ["synthetic-reconciled-step"],
            "methodStates": [
                {
                    "sessionId": "session-synthetic",
                    "assemblyId": "assembly-synthetic",
                    "moduleGeneration": 0,
                    "methodId": "method-synthetic",
                    "sourceHash": "a" * 64,
                    "appliedHash": "a" * 64,
                    "artifactId": None,
                    "appliedPlanId": plan["planId"],
                    "state": "applied",
                }
            ],
            "error": None,
        }


class RecoveryConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-liveloop-recovery-consistency-")
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "host.sqlite3"
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        self.ledger: Ledger | None = Ledger(self.database_path)
        self.artifacts = ArtifactStore(self.ledger, [artifact_root])
        self.task = self.ledger.create_task(
            {
                "sessionId": "session-synthetic",
                "goal": "Reconcile a synthetic interrupted update.",
                "target": "target-synthetic",
                "allowedImpact": synthetic_impact(),
                "acceptance": ["Recovery truth is durable and internally consistent."],
            }
        )

    def tearDown(self) -> None:
        if self.ledger is not None:
            self.ledger.close()
        self.temporary.cleanup()

    def recover(
        self,
        *,
        observed_revision: str,
        reported_runtime_changed: bool | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int, int, ReconciliationProvider]:
        assert self.ledger is not None
        plan = self.ledger.create_plan(
            {
                "taskId": self.task["taskId"],
                "sessionId": self.task["sessionId"],
                "inputSnapshot": "snapshot-synthetic",
                "expectedRuntimeRevision": "revision-before",
                "route": "HOTFIX",
                "state": "prepared",
                "prepareComplete": True,
                "approvalRequired": False,
                "details": {"expectedRuntimeRevisionAfter": observed_revision},
            }
        )
        interrupted = self.ledger.create_job(
            {
                "requestId": f"request-{observed_revision}-{reported_runtime_changed}",
                "operation": "iterate",
                "taskId": self.task["taskId"],
                "planId": plan["planId"],
                "state": "running",
                "stage": "runtime_apply",
                "runtimeChanged": None,
            }
        )
        runtime = ReconciliationProvider(observed_revision, reported_runtime_changed)
        coordinator = UpdateCoordinator(
            self.ledger,
            self.artifacts,
            SavedSourceProvider(),
            runtime,
            runtime_verified=True,
        )
        try:
            recovered = coordinator.recover_pending()
            terminal = next(item for item in recovered if item["jobId"] == interrupted["jobId"])
            evidence_count = len(coordinator.evidence.list_task(self.task["taskId"]))
            method_delta_count = len(
                self.ledger.method_delta(
                    "session-synthetic",
                    "assembly-synthetic",
                    0,
                    {"method-synthetic": "a" * 64},
                )
            )
        finally:
            coordinator.close()

        self.ledger.close()
        self.ledger = None
        reopened = Ledger(self.database_path)
        try:
            durable_job = reopened.get_job(interrupted["jobId"])
            durable_task = reopened.get_task(self.task["taskId"])
            durable_evidence_count = len(EvidenceStore(reopened).list_task(self.task["taskId"]))
        finally:
            reopened.close()
        self.assertEqual(evidence_count, durable_evidence_count)
        return terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime

    def assert_no_replay(self, runtime: ReconciliationProvider) -> None:
        self.assertEqual(0, runtime.apply_calls)
        self.assertEqual(1, runtime.reconcile_calls)
        self.assertEqual(1, runtime.observe_calls)

    def assert_unknown_without_projection(
        self,
        terminal: dict[str, Any],
        durable_job: dict[str, Any],
        durable_task: dict[str, Any],
        evidence_count: int,
        method_delta_count: int,
    ) -> None:
        self.assertEqual("state_unknown", terminal["state"])
        self.assertEqual(terminal, durable_job)
        self.assertEqual("runtime_reconcile", durable_job["stage"])
        self.assertIsNone(durable_job["runtimeChanged"])
        self.assertIsNone(durable_job["result"])
        self.assertEqual("STATE_UNKNOWN", durable_job["error"]["code"])
        self.assertFalse(durable_job["error"]["recoverable"])
        self.assertEqual(0, evidence_count)
        self.assertEqual(1, method_delta_count)
        self.assertTrue(all(value is None for value in durable_task["facts"].values()))

    def test_changed_observation_rejects_completed_unchanged_claim(self) -> None:
        result = self.recover(observed_revision="revision-after", reported_runtime_changed=False)
        terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime = result

        self.assert_no_replay(runtime)
        self.assert_unknown_without_projection(
            terminal,
            durable_job,
            durable_task,
            evidence_count,
            method_delta_count,
        )
        self.assertEqual(
            {
                "expectedRuntimeRevision": "revision-before",
                "observedRuntimeRevision": "revision-after",
                "observedRevisionChanged": True,
                "reportedRuntimeChanged": False,
            },
            durable_job["error"]["details"],
        )

    def test_unchanged_observation_rejects_completed_changed_claim(self) -> None:
        result = self.recover(observed_revision="revision-before", reported_runtime_changed=True)
        terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime = result

        self.assert_no_replay(runtime)
        self.assert_unknown_without_projection(
            terminal,
            durable_job,
            durable_task,
            evidence_count,
            method_delta_count,
        )
        self.assertEqual(
            {
                "expectedRuntimeRevision": "revision-before",
                "observedRuntimeRevision": "revision-before",
                "observedRevisionChanged": False,
                "reportedRuntimeChanged": True,
            },
            durable_job["error"]["details"],
        )

    def test_completed_result_without_attribution_stays_unknown(self) -> None:
        result = self.recover(observed_revision="revision-after", reported_runtime_changed=None)
        terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime = result

        self.assert_no_replay(runtime)
        self.assert_unknown_without_projection(
            terminal,
            durable_job,
            durable_task,
            evidence_count,
            method_delta_count,
        )
        self.assertIsNone(durable_job["error"]["details"]["reportedRuntimeChanged"])

    def test_matching_changed_claim_completes_and_projects_evidence(self) -> None:
        result = self.recover(observed_revision="revision-after", reported_runtime_changed=True)
        terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime = result

        self.assert_no_replay(runtime)
        self.assertEqual("completed", terminal["state"])
        self.assertEqual(terminal, durable_job)
        self.assertTrue(durable_job["runtimeChanged"])
        self.assertIsNone(durable_job["error"])
        self.assertEqual("revision-after", durable_job["result"]["runtimeRevisionAfter"])
        self.assertTrue(durable_task["facts"]["sourceSaved"])
        self.assertTrue(durable_task["facts"]["runtimeMatched"])
        self.assertEqual(2, evidence_count)
        self.assertEqual(0, method_delta_count)

    def test_matching_unchanged_claim_completes_without_inventing_change(self) -> None:
        result = self.recover(observed_revision="revision-before", reported_runtime_changed=False)
        terminal, durable_job, durable_task, evidence_count, method_delta_count, runtime = result

        self.assert_no_replay(runtime)
        self.assertEqual("completed", terminal["state"])
        self.assertEqual(terminal, durable_job)
        self.assertFalse(durable_job["runtimeChanged"])
        self.assertIsNone(durable_job["error"])
        self.assertEqual("revision-before", durable_job["result"]["runtimeRevisionAfter"])
        self.assertTrue(durable_task["facts"]["sourceSaved"])
        self.assertTrue(durable_task["facts"]["runtimeMatched"])
        self.assertEqual(2, evidence_count)
        self.assertEqual(0, method_delta_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
