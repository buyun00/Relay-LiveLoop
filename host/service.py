from __future__ import annotations

import threading
from typing import Any

from .artifacts import ArtifactStore
from .coordinator import UpdateCoordinator
from .errors import CommandError
from .ledger import Ledger
from .lifecycle import HostLifecycle
from .providers import ProviderRegistry
from .result_policy import ProviderResultPolicy
from .validation import canonical_command_hash, validate_command

FACT_KEYS = ("sourceSaved", "runtimeMatched", "checksPassed", "visualReviewed", "freshVerified")

PROVIDER_OPERATIONS = {
    "observe": "observation",
    "source.locate": "source",
    "source.edit": "source",
    "component.preview": "runtime_component",
    "component.revert": "runtime_component",
    "verify": "verification",
    "input.click": "verification",
    "input.text": "verification",
    "baseline.build": "baseline",
    "baseline.import": "baseline",
    "player.start": "player_process",
    "player.attach": "player_process",
    "player.stop": "player_process",
}


def unknown_facts() -> dict[str, None]:
    return {key: None for key in FACT_KEYS}


class CommandService:
    """Deterministic command service shared by HTTP, CLI, and MCP adapters."""

    def __init__(
        self,
        ledger: Ledger,
        artifacts: ArtifactStore,
        providers: ProviderRegistry | None = None,
        coordinator: UpdateCoordinator | None = None,
        result_policy: ProviderResultPolicy | None = None,
        lifecycle: HostLifecycle | None = None,
    ) -> None:
        self.ledger = ledger
        self.artifacts = artifacts
        self.providers = providers or ProviderRegistry()
        self.coordinator = coordinator
        self.result_policy = result_policy or ProviderResultPolicy(ledger, artifacts)
        self.lifecycle = lifecycle or HostLifecycle(ledger)
        self._execute_lock = threading.RLock()

    def _response(
        self,
        request_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        job_id: str | None = None,
        plan_id: str | None = None,
        runtime_changed: bool | None = False,
        facts: dict[str, bool | None] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        timings_ms: dict[str, int | float] | None = None,
    ) -> dict[str, Any]:
        response = {
            "requestId": request_id,
            "status": status,
            "jobId": job_id,
            "planId": plan_id,
            "runtimeChanged": runtime_changed,
            "facts": unknown_facts() if facts is None else {key: facts.get(key) for key in FACT_KEYS},
            "result": result or {},
            "error": error,
            "artifacts": artifacts or [],
            "timingsMs": timings_ms or {},
        }
        return response

    def execute(self, raw_command: Any) -> dict[str, Any]:
        request_id = raw_command.get("requestId", "invalid-request") if isinstance(raw_command, dict) else "invalid-request"
        if not isinstance(request_id, str) or not request_id:
            request_id = "invalid-request"
        try:
            command = validate_command(raw_command)
            request_id = command["requestId"]
        except CommandError as error:
            return self._response(request_id, status="failed", error=error.as_dict(), runtime_changed=error.runtime_changed)

        with self._execute_lock:
            command_hash = canonical_command_hash(command)
            try:
                stored = self.ledger.replay_command(request_id, command_hash)
            except CommandError as error:
                return self._response(
                    request_id,
                    status="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
                    error=error.as_dict(),
                    runtime_changed=error.runtime_changed,
                )
            if stored is not None:
                return stored
            if not self.lifecycle.allows_command(command["operation"]):
                error = CommandError(
                    "CONFLICT",
                    "Host is draining and is not accepting new work.",
                    stage="host_lifecycle",
                    runtime_changed=False,
                    recoverable=True,
                    details={"lifecycle": self.lifecycle.status()},
                )
                return self._response(
                    request_id,
                    status="failed",
                    error=error.as_dict(),
                    runtime_changed=False,
                )
            try:
                disposition, stored = self.ledger.begin_command(
                    request_id,
                    command_hash,
                    command["operation"],
                    command["taskId"],
                )
                if disposition == "replay":
                    assert stored is not None
                    return stored
                response = self._dispatch(command)
            except CommandError as error:
                response = self._response(
                    request_id,
                    status="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
                    error=error.as_dict(),
                    runtime_changed=error.runtime_changed,
                )
                # A same-ID mismatch or an orphaned in-progress command must not overwrite the original ledger row.
                if error.stage == "idempotency":
                    return response
            except Exception as exc:
                error = CommandError(
                    "STATE_UNKNOWN",
                    f"Host failed before a terminal contract result ({type(exc).__name__}).",
                    stage="host",
                    runtime_changed=None,
                    recoverable=False,
                )
                response = self._response(request_id, status="state_unknown", error=error.as_dict(), runtime_changed=None)
            self.ledger.complete_command(request_id, response)
            return response

    def request_shutdown(self, raw_request: Any) -> tuple[dict[str, Any], bool]:
        request_id = raw_request.get("requestId", "invalid-request") if isinstance(raw_request, dict) else "invalid-request"
        if not isinstance(request_id, str) or not request_id:
            request_id = "invalid-request"
        with self._execute_lock:
            try:
                decision, should_schedule = self.lifecycle.request_shutdown(raw_request)
                if decision["accepted"]:
                    return (
                        self._response(
                            request_id,
                            status="accepted",
                            result={"shutdownAccepted": True, "hostLifecycle": decision["lifecycle"]},
                            runtime_changed=False,
                        ),
                        should_schedule,
                    )
                return (
                    self._response(
                        request_id,
                        status="failed",
                        result={"shutdownAccepted": False, "hostLifecycle": decision["lifecycle"]},
                        error=decision["error"],
                        runtime_changed=False,
                    ),
                    False,
                )
            except CommandError as error:
                return (
                    self._response(
                        request_id,
                        status="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
                        error=error.as_dict(),
                        runtime_changed=error.runtime_changed,
                    ),
                    False,
                )

    def _dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        operation = command["operation"]
        request_id = command["requestId"]
        task_id = command["taskId"]
        arguments = command["arguments"]

        if operation == "prepare":
            if self.coordinator is None:
                from .errors import capability_unavailable

                raise capability_unavailable("preparation")
            job = self.coordinator.enqueue_prepare(command)
            return self._response(
                request_id,
                status="accepted",
                result={"job": job},
                job_id=job["jobId"],
                runtime_changed=False,
            )
        if operation == "iterate":
            if self.coordinator is None:
                from .errors import capability_unavailable

                raise capability_unavailable("runtime_apply")
            job = self.coordinator.enqueue_iterate(command)
            return self._response(
                request_id,
                status="accepted",
                result={"job": job},
                job_id=job["jobId"],
                plan_id=job["planId"],
                runtime_changed=False,
            )
        if operation == "status":
            return self._response(request_id, status="completed", result=self.status_summary())
        if operation == "task.open":
            task = self.ledger.create_task(arguments)
            return self._response(request_id, status="completed", result={"taskId": task["taskId"], "task": task}, facts=task["facts"])
        if operation == "task.update":
            task = self.ledger.update_task(task_id, arguments["updates"])
            return self._response(request_id, status="completed", result={"task": task}, facts=task["facts"])
        if operation == "task.show":
            task = self.ledger.get_task(task_id)
            return self._response(request_id, status="completed", result={"task": task}, facts=task["facts"])
        if operation == "task.approve":
            approval = self.ledger.approve_plan(
                task_id,
                arguments["planId"],
                arguments["userConfirmationRef"],
                arguments["approvedImpact"],
            )
            task = self.ledger.get_task(task_id)
            return self._response(request_id, status="completed", result={"approval": approval}, plan_id=arguments["planId"], facts=task["facts"])
        if operation == "job.status":
            job = self.ledger.get_job(arguments["jobId"])
            return self._response(
                request_id,
                status="completed",
                result={"job": job},
                job_id=job["jobId"],
                plan_id=job["planId"],
                runtime_changed=job["runtimeChanged"],
            )
        if operation == "job.cancel":
            cancellation = self.ledger.request_job_cancel(arguments["jobId"])
            job = cancellation["job"]
            return self._response(
                request_id,
                status="completed",
                result=cancellation,
                job_id=job["jobId"],
                plan_id=job["planId"],
                runtime_changed=job["runtimeChanged"],
            )
        if operation == "report":
            task = self.ledger.get_task(task_id)
            return self._response(
                request_id,
                status="completed",
                result={
                    "task": task,
                    "evidence": self.result_policy.evidence.list_task(task_id),
                    "summary": "Report contains persisted facts and their evidence records; omitted review or fresh verification remains unknown.",
                },
                facts=task["facts"],
            )
        capability = PROVIDER_OPERATIONS[operation]
        provider_result = self.providers.execute(capability, operation, command)
        provider_result = self.result_policy.validate(operation, task_id, provider_result, request_id)
        return self._response(
            request_id,
            status=provider_result.get("status", "completed"),
            result=provider_result.get("result", {}),
            error=provider_result.get("error"),
            job_id=provider_result.get("jobId"),
            plan_id=provider_result.get("planId"),
            runtime_changed=provider_result.get("runtimeChanged", False),
            facts=provider_result.get("facts"),
            artifacts=provider_result.get("artifacts"),
            timings_ms=provider_result.get("timingsMs"),
        )

    def close(self) -> None:
        if self.coordinator is not None:
            self.coordinator.close()

    def status_summary(self) -> dict[str, Any]:
        capabilities = {item["capability"]: item for item in self.providers.states()}
        if self.coordinator is not None:
            capabilities.update(self.coordinator.capability_states())
        ledger_summary = self.ledger.summary()
        ledger_summary["counts"]["evidenceRecords"] = self.result_policy.evidence.count()
        return {
            "service": "Relay LiveLoop",
            "protocolVersion": 1,
            "hostLifecycle": self.lifecycle.status(),
            "ledger": ledger_summary,
            "capabilities": [capabilities[key] for key in sorted(capabilities)],
            "runtime": {
                "sessionId": None,
                "runtimeRevision": None,
                "moduleGeneration": None,
                "resourceRelease": None,
                "viewGeneration": None,
            },
        }

    def capability_summary(self) -> dict[str, Any]:
        return {
            "protocolVersion": 1,
            "capabilities": self.status_summary()["capabilities"],
            "hostLifecycle": self.lifecycle.contract(),
        }
