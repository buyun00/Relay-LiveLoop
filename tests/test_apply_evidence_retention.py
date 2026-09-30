from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from host.artifacts import ArtifactStore
from host.coordinator import UpdateCoordinator
from host.errors import CommandError
from host.ledger import Ledger
from host.native_compile_receipt import COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, REQUIRED_COMPILER_INPUT_LIMITATIONS
from tests.test_recovery_consistency_runtime_attribution import ReconciliationProvider, SavedSourceProvider, synthetic_impact


class ApplyEvidenceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-apply-evidence-")
        root = Path(self.temporary.name)
        self.db = root / "ledger.sqlite3"
        self.ledger = Ledger(self.db)
        self.artifacts = ArtifactStore(self.ledger, [root])
        self.task = self.ledger.create_task({
            "sessionId": "session-synthetic", "goal": "Preserve synthetic preparation evidence.",
            "target": "target-synthetic", "allowedImpact": synthetic_impact(), "acceptance": ["Evidence survives restart."],
        })
        self.code_evidence = {
            "compilerInputCoverage": COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
            "compilerInputLimitations": sorted(REQUIRED_COMPILER_INPUT_LIMITATIONS),
            "compileInputReceiptArtifactId": "artifact-synthetic-receipt",
            "compileInputReceiptSha256": "a" * 64,
        }
        self.journal = [{"operation": "synthetic.apply", "status": "acknowledged"}]
        self.coordinator = None

    def tearDown(self):
        if self.coordinator:
            self.coordinator.close()
        self.ledger.close()
        self.temporary.cleanup()

    def create(self, evidence=None, details=None, stage="runtime_apply"):
        plan = self.ledger.create_plan({
            "taskId": self.task["taskId"], "sessionId": self.task["sessionId"],
            "inputSnapshot": "snapshot-synthetic", "expectedRuntimeRevision": "revision-before", "route": "HOTFIX",
            "state": "prepared", "prepareComplete": True, "approvalRequired": False,
            "details": {"preparationEvidence": evidence or self.code_evidence,
                        "expectedRuntimeRevisionAfter": "revision-after", **(details or {})},
        })
        job = self.ledger.create_job({
            "requestId": "request-synthetic-retention", "operation": "iterate", "taskId": self.task["taskId"],
            "planId": plan["planId"], "state": "running", "stage": stage, "runtimeChanged": None,
            "result": {"runtimeStageJournal": self.journal, "runtimeBefore": {"runtimeRevision": "revision-before"},
                       "resumeCommand": {"operation": "iterate"}, "futureProviderEvidence": {"known": True}},
        })
        return plan, job

    def assert_retained(self, job):
        result = job["result"]
        for key, value in self.code_evidence.items():
            self.assertEqual(value, result[key])
        self.assertEqual(self.journal, result["runtimeStageJournal"])
        self.assertEqual({"runtimeRevision": "revision-before"}, result["runtimeBefore"])
        self.assertEqual({"known": True}, result["futureProviderEvidence"])
        reopened = Ledger(self.db)
        try:
            self.assertEqual(result, reopened.get_job(job["jobId"])["result"])
        finally:
            reopened.close()

    def test_task_preflight_failure_retains_receipt_and_journal(self):
        plan, job = self.create(details={"taskUpdatedAt": "different-task-boundary"})
        runtime = ReconciliationProvider("revision-after", True)
        self.coordinator = UpdateCoordinator(self.ledger, self.artifacts, SavedSourceProvider(), runtime, runtime_verified=True)
        with self.assertRaises(CommandError) as failure:
            self.coordinator._apply_plan(job["jobId"], {}, plan)
        self.coordinator._fail_job(job["jobId"], failure.exception)
        self.assertEqual("INPUT_CHANGED", failure.exception.code)
        self.assertEqual(0, runtime.apply_calls)
        self.assert_retained(self.ledger.get_job(job["jobId"]))

    def test_recovery_completed_retains_composite_code_evidence_and_stage_journal(self):
        _, job = self.create(evidence={"schema": "relay.liveloop.composite-preparation-evidence", "code": self.code_evidence})
        runtime = ReconciliationProvider("revision-after", True)
        self.coordinator = UpdateCoordinator(self.ledger, self.artifacts, SavedSourceProvider(), runtime, runtime_verified=True)
        terminal = self.coordinator.recover_pending()[0]
        self.assertEqual("completed", terminal["state"])
        self.assertEqual(0, runtime.apply_calls)
        self.assertEqual(1, runtime.reconcile_calls)
        self.assert_retained(terminal)

    def test_missing_recovery_provider_retains_receipt_and_prior_stage_journal(self):
        _, job = self.create()
        self.coordinator = UpdateCoordinator(self.ledger, self.artifacts, SavedSourceProvider(), None)
        terminal = self.coordinator.recover_pending()[0]
        self.assertEqual("state_unknown", terminal["state"])
        self.assertIsNone(terminal["runtimeChanged"])
        self.assert_retained(terminal)


if __name__ == "__main__":
    unittest.main()
