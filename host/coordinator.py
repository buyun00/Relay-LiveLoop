from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from host.artifacts import ArtifactStore
from host.evidence import EvidenceStore
from host.errors import CommandError, capability_unavailable
from host.ledger import Ledger
from host.native_compile_receipt import (
    COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
    validate_compiler_input_coverage,
)
from host.validation import ID_RE, SHA256_RE

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


def _current_profile_digest(provider: Any, task: dict[str, Any]) -> str | None:
    loader = getattr(provider, "current_profile_digest", None)
    if not callable(loader):
        return None
    digest = loader(task)
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or not SHA256_RE.fullmatch(digest[7:])
    ):
        raise CommandError("CONTRACT_MISMATCH", "Preparation provider returned an invalid compile-profile digest.", stage="prepare", runtime_changed=False, recoverable=False)
    return digest


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
            self._update_job_progress(
                job_id,
                state="completed",
                stage="prepared",
                runtime_changed=False,
                updates={"plan": plan},
            )
        except Exception as exc:
            if self._defer_editor_timeout(job_id, exc):
                return
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
            if self._defer_editor_timeout(job_id, exc):
                return
            self._fail_job(job_id, _provider_error("iterate", exc, runtime_changed=None))

    def _prepare_plan(self, job_id: str, command: dict[str, Any]) -> dict[str, Any]:
        provider = self.preparation_provider
        self._require_preparation()
        self._require_runtime_observation()
        assert provider is not None
        task = self.ledger.get_task(command["taskId"])
        self._validate_task_identity(task, command)
        before = self._observe_runtime(task["sessionId"], task=task)
        assert before is not None
        input_snapshot = provider.current_input_snapshot(task)
        requested_snapshot = command["arguments"].get("inputSnapshot")
        if requested_snapshot is not None and requested_snapshot != input_snapshot:
            raise CommandError("INPUT_CHANGED", "Requested input snapshot differs from current configured source inputs.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        expected_revision = command["arguments"].get("expectedRuntimeRevision") or command.get("context", {}).get("expectedRuntimeRevision")
        if expected_revision is not None and expected_revision != before["runtimeRevision"]:
            raise CommandError("STALE_TARGET", "Requested runtime revision differs from authenticated state.", stage="prepare", runtime_changed=False, recoverable=False)
        profile_loader = getattr(provider, "profile_for_task", None)
        profile = profile_loader(task) if callable(profile_loader) else None
        profile_id = getattr(profile, "profile_id", None)
        profile_digest = _current_profile_digest(provider, task)
        request = {
            "jobId": job_id,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "requestedInputSnapshot": requested_snapshot,
            "inputSnapshot": input_snapshot,
            "expectedRuntimeRevision": before["runtimeRevision"],
            "taskUpdatedAt": task["updatedAt"],
            "runtimeState": dict(before),
            "profileId": profile_id,
        }
        if profile_digest is not None:
            request["profileDigest"] = profile_digest
        coordinator_binding = {
            "schema": "relay.liveloop.prepare-coordinator-binding",
            "version": 2 if profile_digest is not None else 1,
            "jobId": job_id,
            "providerId": provider.provider_id,
            "profileId": profile_id,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "taskUpdatedAt": task["updatedAt"],
            "inputSnapshot": input_snapshot,
            "runtimeState": dict(before),
        }
        if profile_digest is not None:
            coordinator_binding["profileDigest"] = profile_digest
        self._update_job_progress(
            job_id,
            state="running",
            stage="prepare_running",
            runtime_changed=False,
            updates={"prepareCoordinatorBinding": coordinator_binding},
        )
        prepared = provider.prepare(task, request)
        after = self._observe_runtime(task["sessionId"], task=task)
        return self._finalize_prepared_plan(job_id, command, task, provider, prepared, before, after)

    def _finalize_prepared_plan(
        self,
        job_id: str,
        command: dict[str, Any],
        task: dict[str, Any],
        provider: PreparationProvider,
        prepared: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current_task = self.ledger.get_task(task["taskId"])
        if current_task.get("updatedAt") != task.get("updatedAt") or current_task.get("sessionId") != task.get("sessionId"):
            raise CommandError("INPUT_CHANGED", "Task identity or scope changed while Native preparation was compiling.", stage="prepare", runtime_changed=False, recoverable=False)
        profile_digest = _current_profile_digest(provider, task)
        if profile_digest is not None:
            job_record = self.ledger.get_job(job_id)
            coordinator_binding = (job_record.get("result") or {}).get("prepareCoordinatorBinding")
            if (
                not isinstance(coordinator_binding, dict)
                or coordinator_binding.get("version") != 2
                or coordinator_binding.get("profileDigest") != profile_digest
                or not isinstance(prepared, dict)
                or prepared.get("profileDigest") != profile_digest
            ):
                raise CommandError("INPUT_CHANGED", "Prepared result does not match the durable complete compile-profile binding.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        if before is not None and after != before:
            raise CommandError(
                "STATE_UNKNOWN",
                "Runtime state changed during prepare; the provider violated the build-only boundary.",
                stage="prepare",
                runtime_changed=True,
                recoverable=False,
            )
        normalized = self._validate_preparation(prepared, task, provider.current_input_snapshot(task), before, job_id=job_id)
        self._verify_required_artifacts(normalized["artifacts"], normalized["requiredArtifactKinds"])
        receipt_verifier = getattr(provider, "verify_preparation_receipt", None)
        composite_verifier = getattr(provider, "verify_preparation_plan", None)
        if normalized["route"] == "MODULE_AND_ASSET_RELOAD":
            if not callable(composite_verifier):
                raise capability_unavailable("composite_preparation", "The configured provider cannot revalidate a joint code/resource plan.")
            composite_verifier(
                task,
                normalized.get("preparationEvidence"),
                normalized.get("compositeBinding"),
                normalized["artifacts"],
                normalized["inputSnapshot"],
                expected_runtime_state=before,
                job_id=job_id,
            )
        elif callable(receipt_verifier):
            receipt_verifier(
                task,
                normalized.get("preparationEvidence"),
                expected_input_snapshot=normalized["inputSnapshot"],
            )
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
        if normalized["targetGenerationsAfter"] is not None:
            details["targetGenerationsAfter"] = normalized["targetGenerationsAfter"]
        if normalized.get("preparationEvidence") is not None:
            details["preparationEvidence"] = normalized["preparationEvidence"]
        if normalized.get("compositeBinding") is not None:
            details["compositeBinding"] = normalized["compositeBinding"]
        plan_id = self._prepare_plan_id(job_id)
        job_record = self.ledger.get_job(job_id)
        existing_plan_id = (job_record.get("result") or {}).get("preparePlanId")
        if existing_plan_id is not None and existing_plan_id != plan_id:
            raise CommandError("INPUT_CHANGED", "Prepare job is already bound to another plan identity.", stage="prepare", runtime_changed=False, recoverable=False)
        self._update_job_progress(
            job_id,
            state="running",
            stage="prepare_plan_creating",
            runtime_changed=False,
            updates={"preparePlanId": plan_id, "prepareProviderResult": dict(prepared)},
        )
        record = {
            "planId": plan_id,
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
        try:
            existing = self.ledger.get_plan(plan_id)
        except CommandError as exc:
            if exc.stage != "lookup":
                raise
            return self.ledger.create_plan(record)
        if any(existing.get(key) != record.get(key) for key in (
            "taskId", "sessionId", "inputSnapshot", "expectedRuntimeRevision", "route", "state",
            "prepareComplete", "approvalRequired", "details",
        )):
            raise CommandError("INPUT_CHANGED", "Existing deterministic prepare plan differs from its durable candidate.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        return existing

    @staticmethod
    def _prepare_plan_id(job_id: str) -> str:
        digest = hashlib.sha256(("relay.liveloop.prepare-plan/1\n" + job_id).encode("utf-8")).hexdigest()[:32]
        return "plan_" + digest

    def _update_job_progress(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        runtime_changed: bool | None,
        updates: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.ledger.get_job(job_id)
        result = dict(current.get("result") or {})
        result.update(updates)
        return self.ledger.update_job(
            job_id,
            state=state,
            stage=stage,
            runtime_changed=runtime_changed,
            result=result,
            error=error,
        )

    def _defer_editor_timeout(self, job_id: str, exc: Exception) -> bool:
        if not isinstance(exc, CommandError) or exc.code != "TIMEOUT" or exc.stage != "editor_wait":
            return False
        job = self.ledger.get_job(job_id)
        self.ledger.update_job(
            job_id,
            state="running",
            stage="prepare_editor_wait",
            runtime_changed=False,
            result=dict(job.get("result") or {}),
            error=exc.as_dict(),
        )
        return True

    def _recover_prepare_job(self, job: dict[str, Any]) -> None:
        provider = self.preparation_provider
        if provider is None:
            raise CommandError("STATE_UNKNOWN", "Preparation provider is unavailable during recovery.", stage="prepare_reconcile", runtime_changed=None, recoverable=False)
        result_record = job.get("result") if isinstance(job.get("result"), dict) else {}
        command = result_record.get("resumeCommand")
        binding = result_record.get("prepareCoordinatorBinding")
        if not isinstance(command, dict) or not isinstance(binding, dict):
            raise CommandError("STATE_UNKNOWN", "Durable prepare job lacks its immutable task/source/runtime binding.", stage="prepare_reconcile", runtime_changed=None, recoverable=False, details={"automaticCompileReplayAllowed": False})
        task = self.ledger.get_task(job["taskId"])
        self._validate_task_identity(task, command)
        profile_loader = getattr(provider, "profile_for_task", None)
        profile = profile_loader(task) if callable(profile_loader) else None
        expected_profile_id = getattr(profile, "profile_id", None)
        expected_profile_digest = _current_profile_digest(provider, task)
        if (
            binding.get("schema") != "relay.liveloop.prepare-coordinator-binding"
            or binding.get("version") != (2 if expected_profile_digest is not None else 1)
            or binding.get("jobId") != job["jobId"]
            or binding.get("providerId") != provider.provider_id
            or binding.get("profileId") != expected_profile_id
            or binding.get("profileDigest") != expected_profile_digest
            or binding.get("taskId") != task["taskId"]
            or binding.get("sessionId") != task["sessionId"]
            or binding.get("taskUpdatedAt") != task["updatedAt"]
            or not isinstance(binding.get("inputSnapshot"), str)
            or not isinstance(binding.get("runtimeState"), dict)
        ):
            raise CommandError("INPUT_CHANGED", "Recovered prepare binding differs from the current task/profile.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        before = self._observe_runtime(task["sessionId"], task=task)
        if before != binding["runtimeState"]:
            raise CommandError("STALE_TARGET", "Runtime state differs from the precompile recovery binding.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        if provider.current_input_snapshot(task) != binding["inputSnapshot"]:
            raise CommandError("INPUT_CHANGED", "Source inputs changed after the interrupted compile request.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        prepared = provider.reconcile_prepare(job, task)
        if prepared is None and job["stage"] == "prepare_plan_creating":
            saved = result_record.get("prepareProviderResult")
            if isinstance(saved, dict):
                if expected_profile_digest is not None and saved.get("profileDigest") != expected_profile_digest:
                    raise CommandError("INPUT_CHANGED", "Saved prepare result differs from the current complete compile profile.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
                prepared = saved
        if prepared is None:
            raise CommandError(
                "STATE_UNKNOWN",
                "No durable Editor result is available for the exact submitted compile request; automatic compile replay is forbidden.",
                stage="prepare_reconcile",
                runtime_changed=None,
                recoverable=False,
                details={"editorJobId": job["jobId"], "automaticCompileReplayAllowed": False},
            )
        after = self._observe_runtime(task["sessionId"], task=task)
        plan = self._finalize_prepared_plan(job["jobId"], command, task, provider, prepared, before, after)
        if job["operation"] == "prepare":
            self._update_job_progress(
                job["jobId"],
                state="completed",
                stage="prepared",
                runtime_changed=False,
                updates={"plan": plan},
            )
            return
        if job["operation"] != "iterate":
            raise CommandError("STATE_UNKNOWN", "Interrupted prepare operation has an unsupported owner.", stage="prepare_reconcile", runtime_changed=None, recoverable=False)
        self._bind_job_plan(job["jobId"], plan["planId"])
        if plan["approvalRequired"] and not plan["approvalRef"]:
            self._update_job_progress(
                job["jobId"],
                state="completed",
                stage="approval_required",
                runtime_changed=False,
                updates={"status": "approval_required", "plan": plan},
            )
            return
        self._apply_plan(job["jobId"], command, plan)

    def _observe_runtime(self, session_id: str, *, task: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.runtime_provider is None:
            return None
        task_observer = getattr(self.runtime_provider, "observe_task_state", None)
        if task is not None and callable(task_observer):
            state = task_observer(task)
        else:
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
        *,
        job_id: str | None = None,
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
        expected_profile_digest = _current_profile_digest(self.preparation_provider, task)
        if expected_profile_digest is not None:
            required.add("profileDigest")
        value_keys = frozenset(value)
        optional = {"targetGenerationsAfter", "preparationEvidence", "compositeBinding"}
        if not required.issubset(value_keys) or not value_keys.issubset(required | optional):
            raise CommandError("CONTRACT_MISMATCH", "Preparation result fields do not match the provider contract.", stage="prepare")
        for key in ("inputSnapshot", "expectedRuntimeRevision"):
            if not isinstance(value[key], str) or not value[key]:
                raise CommandError("CONTRACT_MISMATCH", f"Preparation result {key} must be a non-empty string.", stage="prepare")
        if value["expectedRuntimeRevisionAfter"] is not None and (
            not isinstance(value["expectedRuntimeRevisionAfter"], str) or not value["expectedRuntimeRevisionAfter"]
        ):
            raise CommandError("CONTRACT_MISMATCH", "Preparation result expectedRuntimeRevisionAfter must be a non-empty string or null.", stage="prepare")
        if value["inputSnapshot"] != current_input_snapshot:
            raise CommandError("INPUT_CHANGED", "Prepared input does not match the provider's current source snapshot.", stage="prepare")
        if expected_profile_digest is not None and value.get("profileDigest") != expected_profile_digest:
            raise CommandError("INPUT_CHANGED", "Prepared result compile-profile digest differs from the server-owned profile.", stage="prepare", runtime_changed=False, recoverable=False)
        if runtime_state is not None and value["expectedRuntimeRevision"] != runtime_state["runtimeRevision"]:
            raise CommandError("STALE_TARGET", "Prepared runtime revision does not match the observed runtime.", stage="prepare")
        if value["route"] not in ROUTES or value["route"] == "BLOCKED":
            raise CommandError("CAPABILITY_UNAVAILABLE", "Preparation did not select an executable verified route.", stage="prepare")
        if value["expectedRuntimeRevisionAfter"] is None and value["route"] not in {"MODULE_RELOAD", "MODULE_AND_ASSET_RELOAD"}:
            raise CommandError("CONTRACT_MISMATCH", "Only module reload routes may use a Player-assigned after revision.", stage="prepare")
        if value["expectedRuntimeRevisionAfter"] is None and "targetGenerationsAfter" not in value:
            raise CommandError("CONTRACT_MISMATCH", "Player-assigned module revision requires frozen targetGenerationsAfter.", stage="prepare")
        if value["route"] == "MODULE_AND_ASSET_RELOAD":
            if not isinstance(value.get("compositeBinding"), dict) or not getattr(
                self.preparation_provider, "is_composite_preparation_provider", False
            ):
                raise CommandError("CAPABILITY_UNAVAILABLE", "MODULE_AND_ASSET_RELOAD requires an explicit composite preparation provider and binding.", stage="prepare", runtime_changed=False, recoverable=False)
        elif "compositeBinding" in value:
            raise CommandError("CONTRACT_MISMATCH", "A non-composite route cannot carry a composite candidate binding.", stage="prepare", runtime_changed=False, recoverable=False)
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
        if "preparationEvidence" in value:
            evidence = value["preparationEvidence"]
            if isinstance(evidence, dict) and evidence.get("schema") == "relay.liveloop.composite-preparation-evidence":
                if value["route"] != "MODULE_AND_ASSET_RELOAD" or not getattr(
                    self.preparation_provider, "is_composite_preparation_provider", False
                ):
                    raise CommandError("CONTRACT_MISMATCH", "Composite preparation evidence is only valid for a composite provider/route.", stage="prepare", runtime_changed=False, recoverable=False)
            else:
                self._validate_preparation_evidence(
                    evidence, value["artifacts"], task, job_id,
                    expected_profile_digest=expected_profile_digest,
                )
        generations = value["targetGenerations"]
        if not isinstance(generations, dict) or set(generations) != {"moduleGeneration", "resourceRelease", "viewGeneration"}:
            raise CommandError("CONTRACT_MISMATCH", "targetGenerations fields do not match the provider contract.", stage="prepare")
        if type(generations["moduleGeneration"]) is not int or generations["moduleGeneration"] < 0:
            raise CommandError("CONTRACT_MISMATCH", "moduleGeneration must be a non-negative integer.", stage="prepare")
        if type(generations["viewGeneration"]) is not int or generations["viewGeneration"] < 0:
            raise CommandError("CONTRACT_MISMATCH", "viewGeneration must be a non-negative integer.", stage="prepare")
        if not isinstance(generations["resourceRelease"], str) or not generations["resourceRelease"]:
            raise CommandError("CONTRACT_MISMATCH", "resourceRelease must be a non-empty string.", stage="prepare")
        if "targetGenerationsAfter" in value:
            after = value["targetGenerationsAfter"]
            if not isinstance(after, dict) or set(after) != {"moduleGeneration", "resourceRelease", "viewGeneration"}:
                raise CommandError("CONTRACT_MISMATCH", "targetGenerationsAfter fields do not match the provider contract.", stage="prepare")
            if type(after["moduleGeneration"]) is not int or after["moduleGeneration"] < 0:
                raise CommandError("CONTRACT_MISMATCH", "targetGenerationsAfter.moduleGeneration must be non-negative.", stage="prepare")
            if type(after["viewGeneration"]) is not int or after["viewGeneration"] < 0:
                raise CommandError("CONTRACT_MISMATCH", "targetGenerationsAfter.viewGeneration must be non-negative.", stage="prepare")
            if not isinstance(after["resourceRelease"], str) or not after["resourceRelease"]:
                raise CommandError("CONTRACT_MISMATCH", "targetGenerationsAfter.resourceRelease must be non-empty.", stage="prepare")
        normalized = dict(value)
        normalized.setdefault("targetGenerationsAfter", None)
        if value["route"] == "MODULE_AND_ASSET_RELOAD":
            verifier = getattr(self.preparation_provider, "verify_prepared_result", None)
            if not callable(verifier):
                raise capability_unavailable("composite_preparation", "The configured provider cannot validate a joint code/resource candidate.")
            verifier(task, normalized, runtime_state, job_id)
        return normalized

    def _validate_preparation_evidence(
        self,
        value: Any,
        artifacts: list[Any],
        task: dict[str, Any],
        job_id: str | None,
        *,
        expected_profile_digest: str | None = None,
    ) -> None:
        expected = {
            "providerId", "profileId", "editorJobId", "editorRequestDigest", "analysisArtifactId",
            "compileInputReceiptArtifactId", "compileInputReceiptSha256", "compileManifestArtifactId",
            "runtimeManifestArtifactId", "editorCandidateArtifactIds", "assemblyDiffs", "compilerInputCoverage",
            "compilerInputLimitations",
        }
        if expected_profile_digest is not None:
            expected.add("profileDigest")
        if not isinstance(value, dict) or set(value) != expected:
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence fields do not match the compile evidence contract.", stage="prepare", runtime_changed=False)
        try:
            validate_compiler_input_coverage(value)
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence must preserve the enumerated-only compiler input boundary.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        provider_id = getattr(self.preparation_provider, "provider_id", None)
        if (
            value["providerId"] != provider_id
            or not isinstance(value["profileId"], str)
            or not ID_RE.fullmatch(value["profileId"])
            or not isinstance(value["editorJobId"], str)
            or not ID_RE.fullmatch(value["editorJobId"])
            or (job_id is not None and value["editorJobId"] != job_id)
            or not isinstance(value["editorRequestDigest"], str)
            or not SHA256_RE.fullmatch(value["editorRequestDigest"])
            or (expected_profile_digest is not None and value.get("profileDigest") != expected_profile_digest)
        ):
            raise CommandError("WRONG_SESSION", "Preparation evidence provider/job/profile binding is invalid.", stage="prepare", runtime_changed=False)
        id_fields = (
            "analysisArtifactId", "compileInputReceiptArtifactId", "compileManifestArtifactId", "runtimeManifestArtifactId",
        )
        for field in id_fields:
            if not isinstance(value[field], str) or not ID_RE.fullmatch(value[field]):
                raise CommandError("CONTRACT_MISMATCH", f"Preparation evidence {field} is invalid.", stage="prepare", runtime_changed=False)
        if not isinstance(value["compileInputReceiptSha256"], str) or not SHA256_RE.fullmatch(value["compileInputReceiptSha256"]):
            raise CommandError("CONTRACT_MISMATCH", "Preparation input-receipt hash is invalid.", stage="prepare", runtime_changed=False)
        candidate_ids = value["editorCandidateArtifactIds"]
        if (
            not isinstance(candidate_ids, list)
            or not 1 <= len(candidate_ids) <= 128
            or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or not {
                value["analysisArtifactId"], value["compileInputReceiptArtifactId"], value["compileManifestArtifactId"]
            }.issubset(candidate_ids)
        ):
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence candidate artifact closure is invalid.", stage="prepare", runtime_changed=False)
        runtime_manifest_ids = {
            item.get("artifactId") for item in artifacts
            if isinstance(item, dict) and item.get("kind") == "runtime_update_manifest"
        }
        if value["runtimeManifestArtifactId"] not in runtime_manifest_ids:
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence does not bind the plan's generated Native manifest.", stage="prepare", runtime_changed=False)
        receipt_artifacts = [
            item for item in artifacts
            if isinstance(item, dict) and item.get("artifactId") == value["compileInputReceiptArtifactId"]
        ]
        if (
            len(receipt_artifacts) != 1
            or receipt_artifacts[0].get("kind") != "native_compile_input_receipt"
            or receipt_artifacts[0].get("sha256") != value["compileInputReceiptSha256"]
        ):
            raise CommandError("CONTRACT_MISMATCH", "Prepared plan does not carry its immutable compiler-input receipt artifact.", stage="prepare", runtime_changed=False)
        for artifact_id in candidate_ids:
            record = self.ledger.get_artifact(artifact_id)
            if record.get("taskId") != task["taskId"] or (job_id is not None and record.get("jobId") != job_id):
                raise CommandError("AUTH_REQUIRED", "Compile evidence artifact ownership differs from its task/job.", stage="prepare", runtime_changed=False)
        diffs = value["assemblyDiffs"]
        if not isinstance(diffs, list) or not 1 <= len(diffs) <= 63:
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence must cover a bounded assembly diff list.", stage="prepare", runtime_changed=False)
        names: set[str] = set()
        for diff in diffs:
            if not isinstance(diff, dict) or set(diff) != {"name", "baselineSha256", "changed"}:
                raise CommandError("CONTRACT_MISMATCH", "Preparation assembly diff fields are invalid.", stage="prepare", runtime_changed=False)
            if (
                not isinstance(diff["name"], str)
                or not diff["name"]
                or diff["name"] in names
                or not isinstance(diff["baselineSha256"], str)
                or not SHA256_RE.fullmatch(diff["baselineSha256"])
                or type(diff["changed"]) is not bool
            ):
                raise CommandError("CONTRACT_MISMATCH", "Preparation assembly diff values are invalid.", stage="prepare", runtime_changed=False)
            names.add(diff["name"])

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
        composite_verifier = None
        if self.preparation_provider is not None:
            current_input = self.preparation_provider.current_input_snapshot(task)
            if current_input != plan["inputSnapshot"]:
                raise CommandError("INPUT_CHANGED", "Source input changed after plan preparation.", stage="iterate")
            receipt_verifier = getattr(self.preparation_provider, "verify_preparation_receipt", None)
            composite_verifier = getattr(self.preparation_provider, "verify_preparation_plan", None)
            if plan.get("route") == "MODULE_AND_ASSET_RELOAD":
                if not callable(composite_verifier):
                    raise capability_unavailable("composite_preparation", "The configured provider cannot revalidate a joint code/resource plan.")
            elif callable(receipt_verifier):
                receipt_verifier(
                    task,
                    plan.get("details", {}).get("preparationEvidence"),
                    expected_input_snapshot=plan["inputSnapshot"],
                )
        self._verify_required_artifacts(plan["details"]["artifacts"], plan["details"]["requiredArtifactKinds"])
        before = self._observe_runtime(task["sessionId"], task=task)
        assert before is not None
        if before["runtimeRevision"] != plan["expectedRuntimeRevision"]:
            raise CommandError("STALE_TARGET", "Runtime revision changed after plan preparation.", stage="iterate")
        targets = plan["details"]["targetGenerations"]
        if any(before[key] != targets[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")):
            raise CommandError("STALE_TARGET", "Runtime generation changed after plan preparation.", stage="iterate")
        if plan.get("route") == "MODULE_AND_ASSET_RELOAD":
            if not callable(composite_verifier):
                raise capability_unavailable("composite_preparation", "The configured provider cannot revalidate a joint code/resource plan.")
            composite_verifier(
                task,
                plan.get("details", {}).get("preparationEvidence"),
                plan.get("details", {}).get("compositeBinding"),
                plan.get("details", {}).get("artifacts"),
                plan["inputSnapshot"],
                expected_runtime_state=before,
                job_id=None,
            )
        current_job = self.ledger.get_job(job_id)
        progress_updates: dict[str, Any] = {"runtimeBefore": dict(before)}
        preparation_evidence = plan.get("details", {}).get("preparationEvidence")
        code_evidence = preparation_evidence
        if isinstance(preparation_evidence, dict) and preparation_evidence.get("schema") == "relay.liveloop.composite-preparation-evidence":
            code_evidence = preparation_evidence.get("code")
        if isinstance(code_evidence, dict):
            try:
                limitations = validate_compiler_input_coverage(code_evidence)
            except ValueError as exc:
                raise CommandError("CONTRACT_MISMATCH", "Apply plan omits the enumerated-only compiler input boundary.", stage="runtime_apply", runtime_changed=False, recoverable=False) from exc
            receipt_id = code_evidence.get("compileInputReceiptArtifactId")
            receipt_sha256 = code_evidence.get("compileInputReceiptSha256")
            if (
                not isinstance(receipt_id, str)
                or not ID_RE.fullmatch(receipt_id)
                or not isinstance(receipt_sha256, str)
                or not SHA256_RE.fullmatch(receipt_sha256)
            ):
                raise CommandError("CONTRACT_MISMATCH", "Apply plan has no valid compile receipt identity for its compiler input limitations.", stage="runtime_apply", runtime_changed=False, recoverable=False)
            progress_updates.update({
                "compilerInputCoverage": COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
                "compilerInputLimitations": list(limitations),
                "compileInputReceiptArtifactId": receipt_id,
                "compileInputReceiptSha256": receipt_sha256,
            })
        if not isinstance((current_job.get("result") or {}).get("runtimeStageJournal"), list):
            progress_updates["runtimeStageJournal"] = []
        self._update_job_progress(
            job_id,
            state="running",
            stage="runtime_apply",
            runtime_changed=None,
            updates=progress_updates,
        )
        try:
            outcome = provider.apply(plan, job_id)
        except Exception as apply_error:
            self._update_job_progress(
                job_id,
                state="running",
                stage="runtime_reconcile",
                runtime_changed=None,
                updates={},
            )
            try:
                outcome = provider.reconcile(plan, self.ledger.get_job(job_id))
            except Exception as reconcile_error:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "Runtime apply failed and reconciliation could not safely continue on the current session.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                    details={
                        "freshSessionRequired": True,
                        "automaticReplayAllowed": False,
                        "applyFailure": type(apply_error).__name__,
                        "reconcileFailure": type(reconcile_error).__name__,
                    },
                ) from reconcile_error
            if outcome is None:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "Runtime apply response was lost and provider reconciliation could not establish the applied state.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                )
        try:
            after = self._observe_runtime(task["sessionId"], task=task)
        except Exception as exc:
            raise CommandError(
                "STATE_UNKNOWN",
                "Post-apply runtime state could not be established; no possibly-dispatched mutation was replayed.",
                stage="runtime_reconcile",
                runtime_changed=None,
                recoverable=False,
                details={"freshSessionRequired": True, "automaticReplayAllowed": False},
            ) from exc
        assert after is not None
        observed_runtime_changed = after["runtimeRevision"] != before["runtimeRevision"]
        try:
            normalized = self._validate_apply_outcome(outcome)
            expected_generations_after = plan["details"].get("targetGenerationsAfter")
            if normalized["status"] == "completed" and expected_generations_after is not None and any(
                after[key] != expected_generations_after[key]
                for key in ("moduleGeneration", "resourceRelease", "viewGeneration")
            ):
                raise CommandError(
                    "STATE_UNKNOWN",
                    "Observed runtime generations differ from the immutable prepared after-state.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                    details={"freshSessionRequired": True},
                )
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
        existing_result = self.ledger.get_job(job_id).get("result") or {}
        result_record = {
            "appliedSteps": normalized["appliedSteps"],
            "runtimeRevisionAfter": normalized["runtimeRevisionAfter"],
            "facts": effective_facts,
        }
        if isinstance(existing_result.get("runtimeBefore"), dict):
            result_record["runtimeBefore"] = existing_result["runtimeBefore"]
        if isinstance(existing_result.get("runtimeStageJournal"), list):
            result_record["runtimeStageJournal"] = existing_result["runtimeStageJournal"]
        for key in (
            "compilerInputCoverage", "compilerInputLimitations", "compileInputReceiptArtifactId", "compileInputReceiptSha256",
        ):
            if key in existing_result:
                result_record[key] = existing_result[key]
        self.ledger.update_job(
            job_id,
            state=state,
            stage="runtime_reconciled" if state == "completed" else "runtime_failed",
            runtime_changed=normalized["runtimeChanged"],
            result=result_record,
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

    @staticmethod
    def _require_recovery_change_consistency(
        plan: dict[str, Any],
        normalized: dict[str, Any],
        observed: dict[str, Any],
    ) -> None:
        if normalized["status"] != "completed":
            return
        observed_revision_changed = observed["runtimeRevision"] != plan["expectedRuntimeRevision"]
        if normalized["runtimeChanged"] is observed_revision_changed:
            return
        raise CommandError(
            "STATE_UNKNOWN",
            "Completed reconciliation does not consistently attribute the observed runtime revision transition to the interrupted apply.",
            stage="runtime_reconcile",
            runtime_changed=None,
            recoverable=False,
            details={
                "expectedRuntimeRevision": plan["expectedRuntimeRevision"],
                "observedRuntimeRevision": observed["runtimeRevision"],
                "observedRevisionChanged": observed_revision_changed,
                "reportedRuntimeChanged": normalized["runtimeChanged"],
            },
        )

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
        target_generations_after = plan["details"].get("targetGenerationsAfter")
        if target_generations_after is not None and any(
            observed[key] != target_generations_after[key]
            for key in ("moduleGeneration", "resourceRelease", "viewGeneration")
        ):
            raise CommandError(
                "STATE_UNKNOWN",
                "Completed runtime result does not match the immutable after-generation target.",
                stage="runtime_reconcile",
                runtime_changed=None,
                recoverable=False,
                details={"freshSessionRequired": True},
            )
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
            reported_after = normalized["runtimeRevisionAfter"]
            if claimed["runtimeMatched"] is True and expected is not None and expected != observed["runtimeRevision"]:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "runtimeMatched=true does not match the prepared expected runtime revision.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                )
            if claimed["runtimeMatched"] is True and expected is None and (
                normalized["status"] != "completed"
                or normalized["runtimeChanged"] is not True
                or reported_after != observed["runtimeRevision"]
                or observed["runtimeRevision"] == plan["expectedRuntimeRevision"]
                or target_generations_after is None
            ):
                raise CommandError(
                    "STATE_UNKNOWN",
                    "Player-assigned revision lacks matching authenticated response and frozen generations.",
                    stage="runtime_reconcile",
                    runtime_changed=None,
                    recoverable=False,
                    details={"freshSessionRequired": True},
                )
            evidence["runtimeMatched"] = {
                "details": {
                    "sessionId": task["sessionId"],
                    "runtimeRevision": observed["runtimeRevision"],
                    "expectedRuntimeRevision": expected,
                    "runtimeRevisionSource": "player_authenticated_response" if expected is None else "prepared_plan",
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
        existing_result = self.ledger.get_job(job_id).get("result") or {}
        self.ledger.update_job(
            job_id,
            state="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
            stage=error.stage,
            runtime_changed=error.runtime_changed,
            result=existing_result,
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
                        observed = self._observe_runtime(task["sessionId"], task=task)
                        assert observed is not None
                        if normalized["runtimeRevisionAfter"] is not None and normalized["runtimeRevisionAfter"] != observed["runtimeRevision"]:
                            raise CommandError("STATE_UNKNOWN", "Reconciled result does not match the observed runtime revision.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
                        self._require_recovery_change_consistency(plan, normalized, observed)
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
            elif job["stage"].startswith("prepare_") and job["stage"] != "prepare_queued":
                try:
                    self._recover_prepare_job(job)
                except Exception as exc:
                    self._fail_job(job["jobId"], _provider_error("prepare_reconcile", exc, runtime_changed=False))
            elif command and job["stage"] == "prepare_queued":
                self._submit(self._run_prepare_job, job["jobId"], command)
            elif command and job["stage"] == "iteration_queued":
                self._submit(self._run_iterate_job, job["jobId"], command)
            else:
                self._fail_job(job["jobId"], CommandError("STATE_UNKNOWN", "Durable job lacks a safe recovery payload.", stage="recovery", runtime_changed=None, recoverable=False))
            recovered.append(self.ledger.get_job(job["jobId"]))
        return recovered
