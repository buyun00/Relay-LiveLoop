from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from typing import Any

from .errors import CommandError
from .ledger import Ledger, utc_now
from .validation import ID_RE, MAX_COMMAND_BYTES, PROTOCOL_VERSION

NON_INTERRUPTIBLE_JOB_STAGES = frozenset({"runtime_apply", "runtime_reconcile"})
COMMANDS_ALLOWED_WHILE_DRAINING = frozenset({"status", "task.show", "job.status", "job.cancel", "report"})


def validate_shutdown_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CommandError("CONTRACT_MISMATCH", "Shutdown request must be an object.", stage="host_lifecycle")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommandError(
            "CONTRACT_MISMATCH",
            f"Shutdown request is not valid JSON data ({type(exc).__name__}).",
            stage="host_lifecycle",
        ) from exc
    if len(encoded) > MAX_COMMAND_BYTES:
        raise CommandError("CONTRACT_MISMATCH", "Shutdown request exceeds the command size limit.", stage="host_lifecycle")
    required = {"protocolVersion", "requestId", "mode", "preservePlayer", "waitForActiveJobs"}
    if set(raw) != required:
        raise CommandError(
            "CONTRACT_MISMATCH",
            "Shutdown request fields do not match the host lifecycle contract.",
            stage="host_lifecycle",
        )
    if type(raw["protocolVersion"]) is not int or raw["protocolVersion"] != PROTOCOL_VERSION:
        raise CommandError(
            "CONTRACT_MISMATCH",
            f"protocolVersion must equal {PROTOCOL_VERSION}.",
            stage="host_lifecycle",
        )
    request_id = raw["requestId"]
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
        raise CommandError("CONTRACT_MISMATCH", "requestId is invalid.", stage="host_lifecycle")
    if raw["mode"] != "graceful":
        raise CommandError(
            "CONTRACT_MISMATCH",
            "Only graceful Host shutdown is supported.",
            stage="host_lifecycle",
        )
    if raw["preservePlayer"] is not True:
        raise CommandError(
            "CONTRACT_MISMATCH",
            "Host lifecycle shutdown requires preservePlayer=true.",
            stage="host_lifecycle",
        )
    if type(raw["waitForActiveJobs"]) is not bool:
        raise CommandError(
            "CONTRACT_MISMATCH",
            "waitForActiveJobs must be a boolean.",
            stage="host_lifecycle",
        )
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "mode": "graceful",
        "preservePlayer": True,
        "waitForActiveJobs": raw["waitForActiveJobs"],
    }


class HostLifecycle:
    """In-process Host drain state backed by authoritative durable job rows."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._lock = threading.RLock()
        self._state = "running"
        self._shutdown_request_id: str | None = None
        self._shutdown_requested_at: str | None = None
        self._wait_for_active_jobs: bool | None = None
        self._decisions: dict[str, tuple[str, dict[str, Any]]] = {}

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "available": True,
            "authentication": "bearer",
            "shutdownEndpoint": "POST /lifecycle/shutdown",
            "modes": ["graceful"],
            "supportsWaitForActiveJobs": True,
            "playerPolicy": "preserve",
        }

    def allows_command(self, operation: str) -> bool:
        with self._lock:
            return self._state == "running" or operation in COMMANDS_ALLOWED_WHILE_DRAINING

    def is_draining(self) -> bool:
        with self._lock:
            return self._state == "draining"

    def status(self) -> dict[str, Any]:
        active_jobs = [self._job_status(job) for job in self._ledger.list_active_jobs()]
        non_interruptible = [job for job in active_jobs if not job["interruptible"]]
        with self._lock:
            draining = self._state == "draining"
            return {
                "state": self._state,
                "acceptingCommands": not draining,
                "shutdownRequested": draining,
                "shutdownRequestId": self._shutdown_request_id,
                "requestedAtUtc": self._shutdown_requested_at,
                "waitForActiveJobs": self._wait_for_active_jobs,
                "safeToExit": draining and len(active_jobs) == 0,
                "playerPolicy": "preserve",
                "activeJobs": active_jobs,
                "nonInterruptibleJobs": non_interruptible,
            }

    def request_shutdown(self, raw: Any) -> tuple[dict[str, Any], bool]:
        request = validate_shutdown_request(raw)
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            prior = self._decisions.get(request["requestId"])
            if prior is not None:
                if prior[0] != digest:
                    raise CommandError(
                        "CONTRACT_MISMATCH",
                        "requestId was already used for a different shutdown request.",
                        stage="idempotency",
                        recoverable=False,
                    )
                return deepcopy(prior[1]), False

            current = self.status()
            if self._state == "running" and current["activeJobs"] and not request["waitForActiveJobs"]:
                decision = {
                    "accepted": False,
                    "lifecycle": current,
                    "error": CommandError(
                        "CONFLICT",
                        "Active Host jobs prevent immediate graceful shutdown.",
                        stage="host_lifecycle",
                        runtime_changed=False,
                        recoverable=True,
                        details={
                            "activeJobs": current["activeJobs"],
                            "nonInterruptibleJobs": current["nonInterruptibleJobs"],
                        },
                    ).as_dict(),
                }
                self._decisions[request["requestId"]] = (digest, deepcopy(decision))
                return decision, False

            should_schedule = self._state == "running"
            if should_schedule:
                self._state = "draining"
                self._shutdown_request_id = request["requestId"]
                self._shutdown_requested_at = utc_now()
                self._wait_for_active_jobs = request["waitForActiveJobs"]
            decision = {"accepted": True, "lifecycle": self.status(), "error": None}
            self._decisions[request["requestId"]] = (digest, deepcopy(decision))
            return decision, should_schedule

    def safe_to_exit(self) -> bool:
        return self.is_draining() and self.status()["safeToExit"]

    @staticmethod
    def _job_status(job: dict[str, Any]) -> dict[str, Any]:
        stage = job["stage"]
        return {
            "jobId": job["jobId"],
            "operation": job["operation"],
            "state": job["state"],
            "stage": stage,
            "interruptible": stage not in NON_INTERRUPTIBLE_JOB_STAGES,
            "cancelRequested": job["cancelRequested"],
            "runtimeChanged": job["runtimeChanged"],
            "updatedAt": job["updatedAt"],
        }
