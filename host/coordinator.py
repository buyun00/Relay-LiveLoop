from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from host.artifacts import ArtifactStore
from host.evidence import EvidenceStore
from host.errors import CommandError, capability_unavailable
from host.ledger import Ledger

ROUTES = frozenset(
    {
        "HOTFIX",
        "VIEW_RELOAD",
        "MODULE_RELOAD",
        "HOTFIX_AND_VIEW",
        "MODULE_AND_ASSET_RELOAD",
        "RESTART_MANAGED",
        "REBUILD_BASELINE",
        "BLOCKED",
    }
)
FACT_KEYS = frozenset(
    {"sourceSaved", "runtimeMatched", "checksPassed", "visualReviewed", "freshVerified"}
)


class PreparationProvider(Protocol):
    provider_id: str

    def current_input_snapshot(self, task: dict[str, Any]) -> str: ...

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]: ...

    def reconcile_prepare(self, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None: ...


class RuntimeUpdateProvider(Protocol):
    provider_id: str

    def observe_state(self, session_id: str) -> dict[str, Any]: ...

    def apply(self, plan: dict[str, Any], job_id: str) -> dict[str, Any]: ...

    def reconcile(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None: ...


def _provider_error(stage: str, exc: Exception, *, runtime_changed: bool | None) -> CommandError:
    if isinstance(exc, CommandError):
        return exc
    return CommandError(
        "STATE_UNKNOWN",
        f"Provider failed without a terminal contract result ({type(exc).__name__}).",
        stage=stage,
        runtime_changed=runtime_changed,
        recoverable=False,
    )


class UpdateCoordinator:
    """Single durable coordinator for prepare/apply/reconcile product operations."""

    def __init__(
        self,
        ledger: Ledger,
        artifacts: ArtifactStore,
        preparation_provider: PreparationProvider | None,
        runtime_provider: RuntimeUpdateProvider | None,
        *,
        preparation_verified: bool = False,
        runtime_verified: bool = False,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.ledger = ledger
        self.artifacts = artifacts
        self.preparation_provider = preparation_provider
        self.runtime_provider = runtime_provider
        self.preparation_verified = preparation_verified
        self.runtime_verified = runtime_verified
        self.evidence = evidence_store or EvidenceStore(ledger)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-liveloop-update")
        self._closed = False
        self._submit_lock = threading.Lock()

    def close(self) -> None:
        with self._submit_lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _submit(self, function: Any, *args: Any) -> None:
        with self._submit_lock:
            if self._closed:
                raise CommandError("CONFLICT", "Update coordinator is closed.", stage="host")
            self._executor.submit(function, *args)

    def enqueue_prepare(self, command: dict[str, Any]) -> dict[str, Any]:
        self._require_preparation()
        self._require_runtime_observation()
        task = self.ledger.get_task(command["taskId"])
        self._validate_task_identity(task, command)
        job = self.ledger.create_job(
            {
                "requestId": command["requestId"],
                "operation": "prepare",
                "taskId": task["taskId"],
                "state": "queued",
                "stage": "prepare_queued",
                "runtimeChanged": False,
                "result": {"resumeCommand": command},
            }
        )
        self._submit(self._run_prepare_job, job["jobId"], command)
        return job

    def enqueue_iterate(self, command: dict[str, Any]) -> dict[str, Any]:
        self._require_runtime_observation()
        if command["arguments"].get("planId") is None:
            self._require_preparation()
        task = self.ledger.get_task(command["taskId"])
        self._validate_task_identity(task, command)
        plan_id = command["arguments"].get("planId")
        if plan_id is not None:
            plan = self.ledger.get_plan(plan_id)
            if plan["taskId"] != task["taskId"]:
                raise CommandError("CONTRACT_MISMATCH", "planId does not belong to taskId.", stage="iterate")
        job = self.ledger.create_job(
            {
                "requestId": command["requestId"],
                "operation": "iterate",
                "taskId": task["taskId"],
                "planId": plan_id,
                "state": "queued",
                "stage": "iteration_queued",
                "runtimeChanged": False,
                "result": {"resumeCommand": command},
            }
        )
        self._submit(self._run_iterate_job, job["jobId"], command)
        return job

    def _require_preparation(self) -> None:
        if self.preparation_provider is None:
            raise capability_unavailable("preparation", "No preparation provider is configured.")
        if not self.preparation_verified:
            raise capability_unavailable("preparation", "The configured preparation provider has no verified capability evidence.")

    def _require_runtime_observation(self) -> None:
        if self.runtime_provider is None:
            raise capability_unavailable("runtime_apply", "No runtime update provider is configured.")
        if not self.runtime_verified:
            raise capability_unavailable("runtime_apply", "The configured runtime provider has no verified capability evidence.")

    def capability_states(self) -> dict[str, dict[str, Any]]:
        return {
            "preparation": {
                "capability": "preparation",
                "available": self.preparation_provider is not None and self.preparation_verified,
                "providerId": getattr(self.preparation_provider, "provider_id", None),
                "reason": None if self.preparation_provider is not None and self.preparation_verified else "No verified preparation provider is configured.",
                "verified": self.preparation_provider is not None and self.preparation_verified,
            },
            "runtime_apply": {
                "capability": "runtime_apply",
                "available": self.runtime_provider is not None and self.runtime_verified,
                "providerId": getattr(self.runtime_provider, "provider_id", None),
                "reason": None if self.runtime_provider is not None and self.runtime_verified else "No verified runtime update provider is configured.",
                "verified": self.runtime_provider is not None and self.runtime_verified,
            },
        }

    def _validate_task_identity(self, task: dict[str, Any], command: dict[str, Any]) -> None:
        session_id = command.get("context", {}).get("sessionId")
        if session_id is not None and session_id != task["sessionId"]:
            raise CommandError("WRONG_SESSION", "Command context session does not match the task session.", stage="identity")

    def _run_prepare_job(self, job_id: str, command: dict[str, Any]) -> None:
        try:
            plan = self._prepare_plan(job_id, command)
            self.ledger.update_job(
                job_id,
                state="completed",
                stage="prepared",
                runtime_changed=False,
                result={"plan": plan},
            )
        except Exception as exc:
            self._fail_job(job_id, _provider_error("prepare", exc, runtime_changed=False))

    def _run_iterate_job(self, job_id: str, command: dict[str, Any]) -> None:
        try:
            plan_id = command["arguments"].get("planId")
            plan = self.ledger.get_plan(plan_id) if plan_id else self._prepare_plan(job_id, command)
            self._bind_job_plan(job_id, plan["planId"])
            if plan["approvalRequired"] and not plan["approvalRef"]:
                self.ledger.update_job(
                    job_id,
                    state="completed",
                    stage="approval_required",
                    runtime_changed=False,
                    result={"status": "approval_required", "plan": plan},
                )
                return
            self._apply_plan(job_id, command, plan)
        except Exception as exc:
            self._fail_job(job_id, _provider_error("iterate", exc, runtime_changed=None))

    def _prepare_plan(self, job_id: str, command: dict[str, Any]) -> dict[str, Any]:
        provider = self.preparation_provider
        self._require_preparation()
        self._require_runtime_observation()
        assert provider is not None
        task = self.ledger.get_task(command["taskId"])
        self._validate_task_identity(task, command)
        before = self._observe_runtime(task["sessionId"])
        self.ledger.update_job(job_id, state="running", stage="prepare_running", runtime_changed=False)
        request = {
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "requestedInputSnapshot": command["arguments"].get("inputSnapshot"),
            "expectedRuntimeRevision": command["arguments"].get("expectedRuntimeRevision")
            or command.get("context", {}).get("expectedRuntimeRevision"),
        }
        prepared = provider.prepare(task, request)
        after = self._observe_runtime(task["sessionId"])
        if before is not None and after != before:
            raise CommandError(
                "STATE_UNKNOWN",
                "Runtime state changed during prepare; the provider violated the build-only boundary.",
                stage="prepare",
                runtime_changed=True,
                recoverable=False,
            )
        normalized = self._validate_preparation(prepared, task, provider.current_input_snapshot(task), before)
        self._verify_required_artifacts(normalized["artifacts"], normalized["requiredArtifactKinds"])
        if not normalized["prepareComplete"]:
            raise CommandError(
                "RESOURCE_BUILD_FAILED",
                "Preparation provider did not produce a complete immutable input set.",
                stage="prepare",
            )
        details = {
            "providerId": provider.provider_id,
            "requiredImpact": normalized["requiredImpact"],
            "artifacts": normalized["artifacts"],
            "requiredArtifactKinds": normalized["requiredArtifactKinds"],
            "affectedModules": normalized["affectedModules"],
            "affectedViews": normalized["affectedViews"],
            "targetGenerations": normalized["targetGenerations"],
            "expectedRuntimeRevisionAfter": normalized["expectedRuntimeRevisionAfter"],
            "taskUpdatedAt": task["updatedAt"],
        }
        return self.ledger.create_plan(
            {
                "taskId": task["taskId"],
                "sessionId": task["sessionId"],
                "inputSnapshot": normalized["inputSnapshot"],
                "expectedRuntimeRevision": normalized["expectedRuntimeRevision"],
                "route": normalized["route"],
                "state": "prepared",
                "prepareComplete": True,
                "approvalRequired": normalized["approvalRequired"],
                "details": details,
            }
        )

    def _observe_runtime(self, session_id: str) -> dict[str, Any] | None:
        if self.runtime_provider is None:
            return None
        state = self.runtime_provider.observe_state(session_id)
        if not isinstance(state, dict):
            raise CommandError("CONTRACT_MISMATCH", "Runtime provider returned a non-object state.", stage="runtime_state")
        required = {"sessionId", "runtimeRevision", "moduleGeneration", "resourceRelease", "viewGeneration"}
        if set(state) != required or state["sessionId"] != session_id:
            raise CommandError("WRONG_SESSION", "Runtime state identity or fields do not match the requested session.", stage="runtime_state")
        for key in ("sessionId", "runtimeRevision", "resourceRelease"):
            if not isinstance(state[key], str) or not state[key]:
                raise CommandError("CONTRACT_MISMATCH", f"Runtime state {key} must be a non-empty string.", stage="runtime_state")
        for key in ("moduleGeneration", "viewGeneration"):
            if type(state[key]) is not int or state[key] < 0:
                raise CommandError("CONTRACT_MISMATCH", f"Runtime state {key} must be a non-negative integer.", stage="runtime_state")
        return dict(state)

    def _validate_preparation(
        self,
        value: Any,
        task: dict[str, Any],
        current_input_snapshot: str,
        runtime_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CommandError("CONTRACT_MISMATCH", "Preparation provider returned a non-object result.", stage="prepare")
        required = {
            "inputSnapshot",
            "expectedRuntimeRevision",
            "expectedRuntimeRevisionAfter",
            "route",
            "requiredImpact",
            "artifacts",
            "requiredArtifactKinds",
            "affectedModules",
            "affectedViews",
            "targetGenerations",
            "approvalRequired",
            "prepareComplete",
        }
        if set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "Preparation result fields do not match the provider contract.", stage="prepare")
        for key in ("inputSnapshot", "expectedRuntimeRevision", "expectedRuntimeRevisionAfter"):
            if not isinstance(value[key], str) or not value[key]:
                raise CommandError("CONTRACT_MISMATCH", f"Preparation result {key} must be a non-empty string.", stage="prepare")
        if value["inputSnapshot"] != current_input_snapshot:
            raise CommandError("INPUT_CHANGED", "Prepared input does not match the provider's current source snapshot.", stage="prepare")
        if runtime_state is not None and value["expectedRuntimeRevision"] != runtime_state["runtimeRevision"]:
            raise CommandError("STALE_TARGET", "Prepared runtime revision does not match the observed runtime.", stage="prepare")
        if value["route"] not in ROUTES or value["route"] == "BLOCKED":
            raise CommandError("CAPABILITY_UNAVAILABLE", "Preparation did not select an executable verified route.", stage="prepare")
        self._validate_impact(value["requiredImpact"])
        if not self._impact_subset(value["requiredImpact"], task["allowedImpact"]):
            value = dict(value)
            value["approvalRequired"] = True
        if type(value["approvalRequired"]) is not bool or type(value["prepareComplete"]) is not bool:
            raise CommandError("CONTRACT_MISMATCH", "Preparation flags must be booleans.", stage="prepare")
        for key in ("requiredArtifactKinds", "affectedModules", "affectedViews"):
            if not isinstance(value[key], list) or len(value[key]) > 128 or any(not isinstance(item, str) or not item for item in value[key]):
                raise CommandError("CONTRACT_MISMATCH", f"Preparation result {key} must be a bounded string array.", stage="prepare")
        if not isinstance(value["artifacts"], list) or len(value["artifacts"]) > 128:
            raise CommandError("CONTRACT_MISMATCH", "Preparation artifacts must be a bounded array.", stage="prepare")
        generations = value["targetGenerations"]
        if not isinstance(generations, dict) or set(generations) != {"moduleGeneration", "resourceRelease", "viewGeneration"}:
            raise CommandError("CONTRACT_MISMATCH", "targetGenerations fields do not match the provider contract.", stage="prepare")
        if type(generations["moduleGeneration"]) is not int or generations["moduleGeneration"] < 0:
            raise CommandError("CONTRACT_MISMATCH", "moduleGeneration must be a non-negative integer.", stage="prepare")
        if type(generations["viewGeneration"]) is not int or generations["viewGeneration"] < 0:
            raise CommandError("CONTRACT_MISMATCH", "viewGeneration must be a non-negative integer.", stage="prepare")
        if not isinstance(generations["resourceRelease"], str) or not generations["resourceRelease"]:
            raise CommandError("CONTRACT_MISMATCH", "resourceRelease must be a non-empty string.", stage="prepare")
        return dict(value)

    @staticmethod
    def _validate_impact(value: Any) -> None:
        required = {"hotfix", "rebuildViews", "reloadModules", "restartPlayer", "buildBaseline"}
        if not isinstance(value, dict) or set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "requiredImpact fields do not match the task impact contract.", stage="prepare")
        if any(type(value[key]) is not bool for key in ("hotfix", "restartPlayer", "buildBaseline")):
            raise CommandError("CONTRACT_MISMATCH", "requiredImpact flags must be booleans.", stage="prepare")
        if any(not isinstance(value[key], list) or any(not isinstance(item, str) or not item for item in value[key]) for key in ("rebuildViews", "reloadModules")):
            raise CommandError("CONTRACT_MISMATCH", "requiredImpact targets must be string arrays.", stage="prepare")

    @staticmethod
    def _impact_subset(candidate: dict[str, Any], boundary: dict[str, Any]) -> bool:
        return (
            all(not candidate[key] or boundary[key] for key in ("hotfix", "restartPlayer", "buildBaseline"))
            and set(candidate["rebuildViews"]).issubset(boundary["rebuildViews"])
            and set(candidate["reloadModules"]).issubset(boundary["reloadModules"])
        )

    def _verify_required_artifacts(self, artifacts: list[Any], required_kinds: list[str]) -> None:
        actual_kinds: set[str] = set()
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {"artifactId", "kind", "sha256", "mediaType", "size"}:
                raise CommandError("CONTRACT_MISMATCH", "Prepared artifact metadata does not match protocol v1.", stage="prepare")
            metadata, stream = self.artifacts.open_verified(item["artifactId"])
            stream.close()
            if metadata != item:
                raise CommandError("INPUT_CHANGED", "Prepared artifact metadata differs from the registered immutable artifact.", stage="prepare")
            actual_kinds.add(item["kind"])
        missing = set(required_kinds) - actual_kinds
        if missing:
            raise CommandError(
                "RESOURCE_BUILD_FAILED",
                "Preparation is missing required artifact kinds.",
                stage="prepare",
                details={"missingKinds": sorted(missing)},
            )

    def _apply_plan(self, job_id: str, command: dict[str, Any], plan: dict[str, Any]) -> None:
        provider = self.runtime_provider
        assert provider is not None
        task = self.ledger.get_task(plan["taskId"])
        if task["updatedAt"] != plan["details"]["taskUpdatedAt"]:
            raise CommandError("INPUT_CHANGED", "Task goal or scope changed after plan preparation.", stage="iterate")
        if self.preparation_provider is not None:
            current_input = self.preparation_provider.current_input_snapshot(task)
            if current_input != plan["inputSnapshot"]:
                raise CommandError("INPUT_CHANGED", "Source input changed after plan preparation.", stage="iterate")
        self._verify_required_artifacts(plan["details"]["artifacts"], plan["details"]["requiredArtifactKinds"])
        before = self._observe_runtime(task["sessionId"])
        assert before is not None
        if before["runtimeRevision"] != plan["expectedRuntimeRevision"]:
            raise CommandError("STALE_TARGET", "Runtime revision changed after plan preparation.", stage="iterate")
        targets = plan["details"]["targetGenerations"]
        if any(before[key] != targets[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")):
            raise CommandError("STALE_TARGET", "Runtime generation changed after plan preparation.", stage="iterate")
        self.ledger.update_job(job_id, state="running", stage="runtime_apply", runtime_changed=None)
        try:
            outcome = provider.apply(plan, job_id)
        except Exception:
            self.ledger.update_job(job_id, state="running", stage="runtime_reconcile", runtime_changed=None)
            outcome = provider.reconcile(plan, self.ledger.get_job(job_id))
            if outcome is None:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "Runtime apply response was lost and provider reconciliation could not establish the applied state.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                )
        after = self._observe_runtime(task["sessionId"])
        assert after is not None
        observed_runtime_changed = after["runtimeRevision"] != before["runtimeRevision"]
        try:
            normalized = self._validate_apply_outcome(outcome)
            if normalized["runtimeRevisionAfter"] is not None and normalized["runtimeRevisionAfter"] != after["runtimeRevision"]:
                raise CommandError("STATE_UNKNOWN", "Apply result does not match reconciled runtime revision.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
            if normalized["runtimeChanged"] is True and not observed_runtime_changed:
                raise CommandError("STATE_UNKNOWN", "Provider reported a runtime change without a new runtime revision.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
            effective_facts = self._record_apply_facts(task, plan, normalized, after)
        except CommandError as error:
            if error.runtime_changed is not None:
                raise CommandError(
                    error.code,
                    error.message,
                    stage=error.stage,
                    runtime_changed=observed_runtime_changed,
                    recoverable=error.recoverable,
                    details=error.details,
                ) from error
            raise
        for method_state in normalized["methodStates"]:
            self.ledger.set_method_state(method_state)
        state = normalized["status"]
        self.ledger.update_job(
            job_id,
            state=state,
            stage="runtime_reconciled" if state == "completed" else "runtime_failed",
            runtime_changed=normalized["runtimeChanged"],
            result={
                "appliedSteps": normalized["appliedSteps"],
                "runtimeRevisionAfter": normalized["runtimeRevisionAfter"],
                "facts": effective_facts,
            },
            error=normalized["error"],
        )

    def _validate_apply_outcome(self, value: Any) -> dict[str, Any]:
        required = {"status", "runtimeChanged", "runtimeRevisionAfter", "facts", "appliedSteps", "methodStates", "error"}
        if not isinstance(value, dict) or set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "Runtime provider result fields do not match the apply contract.", stage="runtime_reconcile")
        if value["status"] not in {"completed", "failed", "state_unknown"}:
            raise CommandError("CONTRACT_MISMATCH", "Runtime provider status is invalid.", stage="runtime_reconcile")
        if value["runtimeChanged"] is not None and type(value["runtimeChanged"]) is not bool:
            raise CommandError("CONTRACT_MISMATCH", "runtimeChanged must be boolean or null.", stage="runtime_reconcile")
        if value["runtimeRevisionAfter"] is not None and (not isinstance(value["runtimeRevisionAfter"], str) or not value["runtimeRevisionAfter"]):
            raise CommandError("CONTRACT_MISMATCH", "runtimeRevisionAfter must be a non-empty string or null.", stage="runtime_reconcile")
        facts = value["facts"]
        if not isinstance(facts, dict) or not facts.keys() <= FACT_KEYS or any(item is not None and type(item) is not bool for item in facts.values()):
            raise CommandError("CONTRACT_MISMATCH", "Runtime facts contain unsupported names or values.", stage="runtime_reconcile")
        if any(facts.get(key) is not None for key in ("checksPassed", "visualReviewed", "freshVerified")):
            raise CommandError("CONTRACT_MISMATCH", "Runtime apply cannot report test, visual-review, or fresh-verification facts.", stage="runtime_reconcile")
        if not isinstance(value["appliedSteps"], list) or any(not isinstance(item, str) or not item for item in value["appliedSteps"]):
            raise CommandError("CONTRACT_MISMATCH", "appliedSteps must be a string array.", stage="runtime_reconcile")
        if not isinstance(value["methodStates"], list) or any(not isinstance(item, dict) for item in value["methodStates"]):
            raise CommandError("CONTRACT_MISMATCH", "methodStates must be an array of method ledger records.", stage="runtime_reconcile")
        if value["status"] == "completed" and value["error"] is not None:
            raise CommandError("CONTRACT_MISMATCH", "A completed runtime result cannot include an error.", stage="runtime_reconcile")
        if value["status"] in {"failed", "state_unknown"} and not isinstance(value["error"], dict):
            raise CommandError("CONTRACT_MISMATCH", "A failed runtime result requires an error object.", stage="runtime_reconcile")
        return dict(value)

    def _record_apply_facts(
        self,
        task: dict[str, Any],
        plan: dict[str, Any],
        normalized: dict[str, Any],
        observed: dict[str, Any],
    ) -> dict[str, bool | None]:
        claimed = normalized["facts"]
        if normalized["status"] != "completed":
            if any(value is not None for value in claimed.values()):
                raise CommandError(
                    "CONTRACT_MISMATCH",
                    "A non-completed runtime result cannot write terminal task facts.",
                    stage="runtime_reconcile",
                )
            return self.ledger.get_task(task["taskId"])["facts"]

        evidence: dict[str, dict[str, Any]] = {}
        if claimed.get("sourceSaved") is not None:
            current_input = self.preparation_provider.current_input_snapshot(task) if self.preparation_provider else None
            if claimed["sourceSaved"] is True and current_input != plan["inputSnapshot"]:
                raise CommandError(
                    "INPUT_CHANGED",
                    "sourceSaved=true does not match the current prepared source snapshot.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                )
            evidence["sourceSaved"] = {
                "details": {
                    "inputSnapshot": plan["inputSnapshot"],
                    "currentInputSnapshot": current_input,
                    "planId": plan["planId"],
                    "preparationProviderId": getattr(self.preparation_provider, "provider_id", None),
                },
                "artifactIds": [],
            }
        if claimed.get("runtimeMatched") is not None:
            expected = plan["details"].get("expectedRuntimeRevisionAfter")
            if claimed["runtimeMatched"] is True and expected != observed["runtimeRevision"]:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "runtimeMatched=true does not match the prepared expected runtime revision.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                )
            evidence["runtimeMatched"] = {
                "details": {
                    "sessionId": task["sessionId"],
                    "runtimeRevision": observed["runtimeRevision"],
                    "expectedRuntimeRevision": expected,
                    "planId": plan["planId"],
                    "moduleGeneration": observed["moduleGeneration"],
                    "resourceRelease": observed["resourceRelease"],
                    "viewGeneration": observed["viewGeneration"],
                },
                "artifactIds": [],
            }
        persisted = self.evidence.apply_task_facts(task["taskId"], "iterate", claimed, evidence)
        return persisted["facts"]

    def _bind_job_plan(self, job_id: str, plan_id: str) -> None:
        with self.ledger.transaction() as connection:
            connection.execute("UPDATE jobs SET plan_id = ? WHERE job_id = ?", (plan_id, job_id))

    def _fail_job(self, job_id: str, error: CommandError) -> None:
        self.ledger.update_job(
            job_id,
            state="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
            stage=error.stage,
            runtime_changed=error.runtime_changed,
            error=error.as_dict(),
        )

    def recover_pending(self) -> list[dict[str, Any]]:
        """Recover durable jobs without repeating any possibly-dispatched runtime apply."""
        with self.ledger.transaction() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE state IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        recovered = []
        for row in rows:
            job = self.ledger.get_job(row["job_id"])
            command = (job.get("result") or {}).get("resumeCommand")
            if job["stage"] in {"runtime_apply", "runtime_reconcile"}:
                if self.runtime_provider is None or not job["planId"]:
                    self._fail_job(job["jobId"], CommandError("STATE_UNKNOWN", "Runtime reconciliation provider or plan is unavailable after restart.", stage="runtime_reconcile", runtime_changed=None, recoverable=False))
                else:
                    plan = self.ledger.get_plan(job["planId"])
                    try:
                        outcome = self.runtime_provider.reconcile(plan, job)
                        if outcome is None:
                            raise CommandError("STATE_UNKNOWN", "Runtime provider cannot reconcile the interrupted apply.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
                        normalized = self._validate_apply_outcome(outcome)
                        task = self.ledger.get_task(plan["taskId"])
                        observed = self._observe_runtime(task["sessionId"])
                        assert observed is not None
                        if normalized["runtimeRevisionAfter"] is not None and normalized["runtimeRevisionAfter"] != observed["runtimeRevision"]:
                            raise CommandError("STATE_UNKNOWN", "Reconciled result does not match the observed runtime revision.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
                        effective_facts = self._record_apply_facts(task, plan, normalized, observed)
                        for method_state in normalized["methodStates"]:
                            self.ledger.set_method_state(method_state)
                        self.ledger.update_job(
                            job["jobId"],
                            state=normalized["status"],
                            stage="runtime_reconciled",
                            runtime_changed=normalized["runtimeChanged"],
                            result={"appliedSteps": normalized["appliedSteps"], "runtimeRevisionAfter": normalized["runtimeRevisionAfter"], "facts": effective_facts},
                            error=normalized["error"],
                        )
                    except Exception as exc:
                        self._fail_job(job["jobId"], _provider_error("runtime_reconcile", exc, runtime_changed=None))
            elif job["stage"] in {"prepare_running"}:
                task = self.ledger.get_task(job["taskId"])
                result = self.preparation_provider.reconcile_prepare(job, task) if self.preparation_provider else None
                if result is None:
                    self._fail_job(job["jobId"], CommandError("STATE_UNKNOWN", "Preparation state cannot be reconciled after restart.", stage="prepare_reconcile", runtime_changed=False, recoverable=False))
                else:
                    self._fail_job(job["jobId"], CommandError("STATE_UNKNOWN", "Preparation produced recoverable data but requires a fresh immutable plan request.", stage="prepare_reconcile", runtime_changed=False, recoverable=True, details={"providerResult": result}))
            elif command and job["stage"] == "prepare_queued":
                self._submit(self._run_prepare_job, job["jobId"], command)
            elif command and job["stage"] == "iteration_queued":
                self._submit(self._run_iterate_job, job["jobId"], command)
            else:
                self._fail_job(job["jobId"], CommandError("STATE_UNKNOWN", "Durable job lacks a safe recovery payload.", stage="recovery", runtime_changed=None, recoverable=False))
            recovered.append(self.ledger.get_job(job["jobId"]))
        return recovered
