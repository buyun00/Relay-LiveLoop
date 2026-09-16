from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from host.artifacts import ArtifactStore
from host.errors import CommandError
from host.evidence import EvidenceStore
from host.ledger import Ledger
from host.validation import ID_RE

FACT_KEYS = {"sourceSaved", "runtimeMatched", "checksPassed", "visualReviewed", "freshVerified"}
ERROR_CODES = {
    "CAPABILITY_UNAVAILABLE",
    "AUTH_REQUIRED",
    "CONTRACT_MISMATCH",
    "WRONG_SESSION",
    "STALE_TARGET",
    "INPUT_CHANGED",
    "COMPILE_FAILED",
    "RESOURCE_BUILD_FAILED",
    "APPROVAL_REQUIRED",
    "UNLOAD_REFUSED",
    "RESTORE_FAILED",
    "STATE_UNKNOWN",
    "INVALID_REQUEST",
    "NOT_FOUND",
    "CONFLICT",
    "INTERNAL_ERROR",
}
MAX_PROVIDER_RESULT_BYTES = 512 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ProviderResultPolicy:
    """Validates provider results before they can change Host facts or cross an interface."""

    def __init__(self, ledger: Ledger, artifacts: ArtifactStore):
        self.ledger = ledger
        self.artifacts = artifacts
        self.evidence = EvidenceStore(ledger)

    def bind_fresh_frame(
        self,
        operation: str,
        command: dict[str, Any],
        value: Any,
    ) -> Any:
        """Register and normalize a Player fresh-frame receipt before result validation.

        The Player supplies a FreshFrameArtifact-shaped object and the receipt's
        task/session/launch/runtime/target/owner fields.  The Host checks those
        bindings against the command and task ledger, then registers the file
        through ArtifactStore so the configured roots and immutable hash boundary
        remain authoritative.  The Player's source artifact id is never treated
        as a Host artifact id.
        """
        arguments = command.get("arguments", {}) if isinstance(command, dict) else {}
        if operation != "verify" or not isinstance(arguments, dict) or arguments.get("requireFreshFrame") is not True:
            return value
        if not isinstance(value, dict):
            self._mismatch("Fresh-frame provider result must be an object.")
        if value.get("status") != "completed":
            return value
        result = value.get("result")
        if not isinstance(result, dict):
            self._mismatch("A completed fresh-frame result must contain an object result.")
        task_id = command.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            self._mismatch("A fresh-frame result requires command taskId.")
        context = command.get("context", {})
        if not isinstance(context, dict):
            self._mismatch("A fresh-frame command context must be an object.")
        for key in ("sessionId", "expectedRuntimeRevision", "expectedLaunchId"):
            if not isinstance(context.get(key), str) or not context[key]:
                self._mismatch(f"Fresh-frame command context.{key} is required.")

        task = self.ledger.get_task(task_id)
        binding = self._fresh_binding(result)
        if binding["taskId"] != task_id:
            self._mismatch("Fresh-frame result taskId does not match the command task.")
        if binding["sessionId"] != context["sessionId"] or binding["sessionId"] != task["sessionId"]:
            raise CommandError("WRONG_SESSION", "Fresh-frame result session does not match the command task.", stage="provider_result")
        for key in ("launchId", "runtimeRevision"):
            context_key = "expectedLaunchId" if key == "launchId" else "expectedRuntimeRevision"
            if binding[key] != context[context_key]:
                self._mismatch(f"Fresh-frame result {key} does not match command context.{context_key}.")
        if binding["ownerGeneration"] != arguments["expectedOwnerGeneration"]:
            raise CommandError("STALE_TARGET", "Fresh-frame owner generation does not match the request.", stage="provider_result", recoverable=True)
        if binding["targetId"] != arguments["targetId"]:
            raise CommandError("STALE_TARGET", "Fresh-frame target does not match the request.", stage="provider_result", recoverable=True)

        frame = self._fresh_frame(result.get("freshFrame"))
        source_artifact_id = frame["artifactId"]
        if source_artifact_id != arguments["frameArtifactId"]:
            self._mismatch("Fresh-frame artifactId does not match arguments.frameArtifactId.")
        if frame["runtimeRevision"] != binding["runtimeRevision"]:
            self._mismatch("Fresh-frame runtimeRevision does not match its result binding.")
        if frame["viewportGeneration"] != arguments["expectedViewportGeneration"]:
            raise CommandError("STALE_TARGET", "Fresh-frame viewport generation does not match the request.", stage="provider_result", recoverable=True)
        if frame["frame"] <= arguments["minimumFrameExclusive"]:
            raise CommandError("STALE_TARGET", "Fresh-frame number is not newer than the requested baseline.", stage="provider_result", recoverable=True)
        if frame["width"] > arguments["maximumWidth"] or frame["height"] > arguments["maximumHeight"]:
            self._mismatch("Fresh-frame dimensions exceed the requested bounds.")
        if not frame["fresh"]:
            self._mismatch("Fresh-frame artifact must assert fresh=true after binding checks.")
        facts = value.get("facts")
        if isinstance(facts, dict) and facts.get("freshVerified") is True:
            fresh_verification = result.get("freshVerification")
            if (
                not isinstance(fresh_verification, dict)
                or fresh_verification.get("checkSetId") != arguments["checkSetId"]
                or not isinstance(fresh_verification.get("evidenceArtifactIds"), list)
                or source_artifact_id not in fresh_verification["evidenceArtifactIds"]
            ):
                self._mismatch("freshVerified=true must bind the requested checkSetId to the fresh-frame artifact.")

        registered = self.artifacts.register(
            frame["path"],
            kind=frame["kind"],
            expected_sha256=frame["sha256"],
            expected_size=frame["size"],
            media_type=frame["mediaType"],
            task_id=task_id,
        )
        if registered["size"] != frame["size"]:
            raise CommandError("INPUT_CHANGED", "Fresh-frame file size differs from the provider receipt.", stage="artifact", recoverable=False)

        normalized = deepcopy(value)
        normalized_result = deepcopy(result)
        normalized_frame = dict(frame)
        normalized_frame["artifactId"] = registered["artifactId"]
        normalized_result["freshFrame"] = normalized_frame
        normalized_result["evidence"] = self._normalize_fresh_evidence(
            normalized_result.get("evidence"), source_artifact_id, registered["artifactId"], frame["sha256"]
        )
        self._replace_known_evidence_references(normalized_result, source_artifact_id, registered["artifactId"])
        normalized["result"] = normalized_result
        normalized["artifacts"] = self._normalize_registered_artifacts(
            normalized.get("artifacts", []), source_artifact_id, registered
        )
        return normalized

    def _fresh_binding(self, result: dict[str, Any]) -> dict[str, Any]:
        required = {
            "taskId",
            "sessionId",
            "launchId",
            "runtimeRevision",
            "ownerGeneration",
            "targetId",
        }
        missing = required - result.keys()
        if missing:
            self._mismatch(f"Fresh-frame result is missing binding fields: {', '.join(sorted(missing))}.")
        binding: dict[str, Any] = {}
        for key in ("taskId", "sessionId", "launchId", "runtimeRevision", "targetId"):
            value = result[key]
            if not isinstance(value, str) or not value or len(value) > 128:
                self._mismatch(f"fresh-frame result {key} must be a bounded non-empty string.")
            if key != "runtimeRevision" and not ID_RE.fullmatch(value):
                self._mismatch(f"fresh-frame result {key} contains unsupported characters.")
            binding[key] = value
        if type(result["ownerGeneration"]) is not int or not 0 <= result["ownerGeneration"] <= 2**63 - 1:
            self._mismatch("fresh-frame result ownerGeneration must be a bounded non-negative integer.")
        binding["ownerGeneration"] = result["ownerGeneration"]
        return binding

    def _fresh_frame(self, value: Any) -> dict[str, Any]:
        fields = {
            "artifactId",
            "kind",
            "mediaType",
            "path",
            "sha256",
            "size",
            "frame",
            "width",
            "height",
            "fresh",
            "runtimeRevision",
            "viewportGeneration",
            "publishedAtUnixMilliseconds",
        }
        frame = self._exact_object(value, fields, "freshFrame")
        for key in ("artifactId", "kind"):
            if not isinstance(frame[key], str) or not ID_RE.fullmatch(frame[key]):
                self._mismatch(f"freshFrame.{key} contains unsupported characters.")
        for key in ("mediaType", "path", "runtimeRevision"):
            if not isinstance(frame[key], str) or not frame[key] or len(frame[key]) > 4096:
                self._mismatch(f"freshFrame.{key} must be a bounded non-empty string.")
        if not frame["mediaType"].lower().startswith("image/"):
            self._mismatch("freshFrame.mediaType must be an image media type.")
        if not isinstance(frame["sha256"], str) or not SHA256_RE.fullmatch(frame["sha256"]):
            self._mismatch("freshFrame.sha256 must be a lowercase SHA-256 value.")
        for key in ("size", "frame", "width", "height", "viewportGeneration", "publishedAtUnixMilliseconds"):
            if type(frame[key]) is not int or frame[key] < 0:
                self._mismatch(f"freshFrame.{key} must be a non-negative integer.")
        if frame["size"] <= 0 or frame["width"] <= 0 or frame["height"] <= 0 or frame["viewportGeneration"] <= 0 or frame["publishedAtUnixMilliseconds"] <= 0:
            self._mismatch("freshFrame size, dimensions, viewport generation, and publication time must be positive.")
        if type(frame["fresh"]) is not bool:
            self._mismatch("freshFrame.fresh must be a boolean.")
        return frame

    def _normalize_registered_artifacts(
        self,
        value: Any,
        source_artifact_id: str,
        registered: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            self._mismatch("Provider artifacts must be an array.")
        normalized: list[dict[str, Any]] = []
        replaced = False
        required = {"artifactId", "kind", "sha256", "mediaType", "size"}
        for item in value:
            if isinstance(item, dict) and item.get("artifactId") == source_artifact_id:
                if set(item) != required:
                    self._mismatch("Provider fresh-frame artifact metadata does not match protocol v1.")
                if any(item[key] != registered[key] for key in ("kind", "sha256", "mediaType", "size")):
                    self._mismatch("Provider fresh-frame artifact metadata differs from the registered file.")
                normalized.append(registered)
                replaced = True
            else:
                normalized.append(item)
        if not replaced:
            normalized.append(registered)
        return normalized

    def _normalize_fresh_evidence(
        self,
        value: Any,
        source_artifact_id: str,
        registered_artifact_id: str,
        expected_sha256: str,
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 128:
            self._mismatch("Provider evidence must be a bounded array.")
        normalized: list[dict[str, Any]] = []
        fields = {"kind", "stage", "artifactId", "sha256", "detail"}
        for item in value:
            if not isinstance(item, dict) or not item.keys() <= fields or "kind" not in item or "stage" not in item:
                self._mismatch("Provider evidence fields do not match the neutral evidence contract.")
            evidence = dict(item)
            for key in ("kind", "stage"):
                if not isinstance(evidence[key], str) or not evidence[key] or len(evidence[key]) > 128:
                    self._mismatch(f"Provider evidence {key} must be a bounded non-empty string.")
            if evidence.get("artifactId") is not None:
                if not isinstance(evidence["artifactId"], str) or not ID_RE.fullmatch(evidence["artifactId"]):
                    self._mismatch("Provider evidence artifactId contains unsupported characters.")
                if evidence["artifactId"] == source_artifact_id:
                    evidence["artifactId"] = registered_artifact_id
                    if evidence.get("sha256") not in {None, expected_sha256}:
                        self._mismatch("Provider evidence sha256 differs from the registered frame.")
            if evidence.get("sha256") is not None and not SHA256_RE.fullmatch(evidence["sha256"]):
                self._mismatch("Provider evidence sha256 must be a lowercase SHA-256 value.")
            if evidence.get("detail") is not None and (not isinstance(evidence["detail"], str) or len(evidence["detail"]) > 4096):
                self._mismatch("Provider evidence detail must be a bounded string or null.")
            normalized.append(evidence)
        return normalized

    @staticmethod
    def _replace_known_evidence_references(result: dict[str, Any], source_artifact_id: str, registered_artifact_id: str) -> None:
        checks = result.get("checks")
        if isinstance(checks, list):
            for check in checks:
                if isinstance(check, dict) and isinstance(check.get("evidenceArtifactIds"), list):
                    check["evidenceArtifactIds"] = [
                        registered_artifact_id if item == source_artifact_id else item
                        for item in check["evidenceArtifactIds"]
                    ]
        review = result.get("visualReview")
        if isinstance(review, dict) and review.get("artifactId") == source_artifact_id:
            review["artifactId"] = registered_artifact_id
        fresh = result.get("freshVerification")
        if isinstance(fresh, dict) and isinstance(fresh.get("evidenceArtifactIds"), list):
            fresh["evidenceArtifactIds"] = [
                registered_artifact_id if item == source_artifact_id else item
                for item in fresh["evidenceArtifactIds"]
            ]

    def validate(
        self,
        operation: str,
        task_id: str | None,
        value: Any,
        request_id: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            self._mismatch("Provider result must be an object.")
        allowed = {"status", "result", "error", "jobId", "planId", "runtimeChanged", "facts", "artifacts", "timingsMs"}
        unknown = value.keys() - allowed
        if unknown:
            self._mismatch(f"Provider result has unknown fields: {', '.join(sorted(unknown))}.")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            self._mismatch("Provider result is not finite JSON data.")
        if len(encoded) > MAX_PROVIDER_RESULT_BYTES:
            self._mismatch("Provider result exceeds the configured size limit.")
        status = value.get("status")
        if status not in {"accepted", "completed", "approval_required", "failed", "state_unknown"}:
            self._mismatch("Provider result status is invalid.")
        runtime_changed = value.get("runtimeChanged", False)
        if runtime_changed is not None and type(runtime_changed) is not bool:
            self._mismatch("runtimeChanged must be boolean or null.")
        result = value.get("result", {})
        if not isinstance(result, dict):
            self._mismatch("Provider result.result must be an object.")
        job_id = self._nullable_id(value.get("jobId"), "jobId")
        plan_id = self._nullable_id(value.get("planId"), "planId")
        error = self._error(value.get("error"))
        if status in {"failed", "state_unknown"} and error is None:
            self._mismatch("A failed or state_unknown provider result requires error details.")
        if status not in {"failed", "state_unknown"} and error is not None:
            self._mismatch("A successful provider result cannot include an error.")
        if status == "accepted" and job_id is None:
            self._mismatch("An accepted provider result requires jobId.")
        if status == "approval_required" and plan_id is None:
            self._mismatch("An approval_required provider result requires planId.")
        if status in {"accepted", "approval_required"} and runtime_changed is not False:
            self._mismatch("Accepted and approval_required results must report runtimeChanged=false.")
        facts = self._facts(value.get("facts", {}))
        if status in {"accepted", "approval_required"} and any(item is not None for item in facts.values()):
            self._mismatch("Accepted and approval_required results cannot claim terminal task facts.")
        if status == "accepted":
            assert job_id is not None
            self._require_job(job_id, operation, task_id, plan_id, request_id)
        if plan_id is not None:
            self._require_plan(plan_id, task_id)
        artifacts = self._artifacts(value.get("artifacts", []), task_id)
        timings = self._timings(value.get("timingsMs", {}))
        evidence = self._validate_fact_evidence(task_id, facts, result)
        if task_id is not None:
            task = self.evidence.apply_task_facts(task_id, operation, facts, evidence)
            facts = task["facts"]
        return {
            "status": status,
            "result": result,
            "error": error,
            "jobId": job_id,
            "planId": plan_id,
            "runtimeChanged": runtime_changed,
            "facts": facts,
            "artifacts": artifacts,
            "timingsMs": timings,
        }

    @staticmethod
    def _mismatch(message: str) -> None:
        raise CommandError("CONTRACT_MISMATCH", message, stage="provider_result")

    def _nullable_id(self, value: Any, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            self._mismatch(f"{name} must be a non-empty bounded string or null.")
        return value

    def _error(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            self._mismatch("error must be an object or null.")
        required = {"code", "stage", "message", "recoverable"}
        optional = {"details"}
        if not required <= value.keys() or not value.keys() <= required | optional:
            self._mismatch("error fields do not match protocol v1.")
        if value["code"] not in ERROR_CODES:
            self._mismatch("error.code is not a protocol v1 error code.")
        if not isinstance(value["stage"], str) or not value["stage"]:
            self._mismatch("error.stage must be a non-empty string.")
        if not isinstance(value["message"], str) or not value["message"]:
            self._mismatch("error.message must be a non-empty string.")
        if type(value["recoverable"]) is not bool:
            self._mismatch("error.recoverable must be a boolean.")
        details = value.get("details", {})
        if not isinstance(details, dict):
            self._mismatch("error.details must be an object.")
        return {**value, "details": details}

    def _facts(self, value: Any) -> dict[str, bool | None]:
        if not isinstance(value, dict) or not value.keys() <= FACT_KEYS:
            self._mismatch("facts contains unsupported names.")
        if any(item is not None and type(item) is not bool for item in value.values()):
            self._mismatch("facts values must be boolean or null.")
        return {key: value.get(key) for key in FACT_KEYS}

    def _artifacts(self, value: Any, task_id: str | None) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 128:
            self._mismatch("artifacts must be a bounded array.")
        normalized = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"artifactId", "kind", "sha256", "mediaType", "size"}:
                self._mismatch("Artifact metadata does not match protocol v1.")
            metadata, stream = self.artifacts.open_verified(item["artifactId"])
            stream.close()
            if metadata != item:
                raise CommandError("INPUT_CHANGED", "Provider artifact metadata differs from its registered immutable file.", stage="provider_result")
            stored = self.ledger.get_artifact(item["artifactId"])
            if task_id is not None and stored["taskId"] not in {None, task_id}:
                raise CommandError("AUTH_REQUIRED", "Artifact is registered to a different task.", stage="provider_result")
            normalized.append(item)
        return normalized

    def _timings(self, value: Any) -> dict[str, int | float]:
        if not isinstance(value, dict) or len(value) > 128:
            self._mismatch("timingsMs must be a bounded object.")
        for key, item in value.items():
            if not isinstance(key, str) or not key or type(item) not in {int, float} or item < 0:
                self._mismatch("timingsMs values must be non-negative numbers.")
        return dict(value)
    def _validate_fact_evidence(
        self,
        task_id: str | None,
        facts: dict[str, bool | None],
        result: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        evidence: dict[str, dict[str, Any]] = {}
        if task_id is None and any(value is not None for value in facts.values()):
            self._mismatch("Task facts require taskId.")
        if facts.get("sourceSaved") is True:
            source = self._exact_object(result.get("sourceEvidence"), {"inputSnapshot", "savedHash", "providerId"}, "sourceEvidence")
            if not isinstance(source["inputSnapshot"], str) or not source["inputSnapshot"]:
                self._mismatch("sourceEvidence.inputSnapshot must be a non-empty string.")
            if not isinstance(source["savedHash"], str) or not SHA256_RE.fullmatch(source["savedHash"]):
                self._mismatch("sourceEvidence.savedHash must be a lowercase SHA-256 value.")
            if not isinstance(source["providerId"], str) or not source["providerId"]:
                self._mismatch("sourceEvidence.providerId must be a non-empty string.")
            evidence["sourceSaved"] = {"details": source, "artifactIds": []}
        if facts.get("runtimeMatched") is True:
            runtime = self._exact_object(result.get("runtimeEvidence"), {"sessionId", "runtimeRevision", "planId"}, "runtimeEvidence")
            self._require_task_session(task_id, runtime["sessionId"])
            if not isinstance(runtime["runtimeRevision"], str) or not runtime["runtimeRevision"]:
                self._mismatch("runtimeEvidence.runtimeRevision must be a non-empty string.")
            if not isinstance(runtime["planId"], str) or not runtime["planId"]:
                self._mismatch("runtimeEvidence.planId must be a non-empty string.")
            self._require_plan(runtime["planId"], task_id)
            evidence["runtimeMatched"] = {"details": runtime, "artifactIds": []}
        if facts.get("checksPassed") is True:
            checks = result.get("checks")
            if not isinstance(checks, list) or not checks:
                self._mismatch("checksPassed=true requires one or more check records.")
            artifact_ids: list[str] = []
            for check in checks:
                normalized = self._exact_object(check, {"checkId", "status", "evidenceArtifactIds"}, "check")
                if not isinstance(normalized["checkId"], str) or not normalized["checkId"]:
                    self._mismatch("check.checkId must be a non-empty string.")
                if normalized["status"] != "passed" or not isinstance(normalized["evidenceArtifactIds"], list):
                    self._mismatch("checksPassed=true requires every check status to be passed.")
                artifact_ids.extend(self._validate_evidence_artifact_ids(normalized["evidenceArtifactIds"], task_id))
            evidence["checksPassed"] = {"details": {"checks": checks}, "artifactIds": artifact_ids}
        if facts.get("visualReviewed") is True:
            review = self._exact_object(result.get("visualReview"), {"reviewer", "artifactId", "conclusion", "reviewedAt"}, "visualReview")
            artifact_ids = self._validate_evidence_artifact_ids([review["artifactId"]], task_id)
            for key in ("reviewer", "conclusion", "reviewedAt"):
                if not isinstance(review[key], str) or not review[key]:
                    self._mismatch(f"visualReview.{key} must be a non-empty string.")
            artifact = self.ledger.get_artifact(review["artifactId"])
            if not artifact["mediaType"].lower().startswith("image/"):
                self._mismatch("visualReview.artifactId must reference an image artifact.")
            evidence["visualReviewed"] = {"details": review, "artifactIds": artifact_ids}
        if facts.get("freshVerified") is True:
            fresh = self._exact_object(result.get("freshVerification"), {"sessionId", "baselineId", "checkSetId", "evidenceArtifactIds"}, "freshVerification")
            self._require_task_session(task_id, fresh["sessionId"])
            for key in ("baselineId", "checkSetId"):
                if not isinstance(fresh[key], str) or not fresh[key]:
                    self._mismatch(f"freshVerification.{key} must be a non-empty string.")
            artifact_ids = self._validate_evidence_artifact_ids(fresh["evidenceArtifactIds"], task_id)
            evidence["freshVerified"] = {"details": fresh, "artifactIds": artifact_ids}
        for fact, outcome in facts.items():
            if outcome is False:
                evidence[fact] = {"details": {"providerResult": "explicit_false"}, "artifactIds": []}
        return evidence

    def _require_task_session(self, task_id: str | None, session_id: Any) -> None:
        if not isinstance(session_id, str) or not session_id:
            self._mismatch("Evidence sessionId must be a non-empty string.")
        if task_id is None or self.ledger.get_task(task_id)["sessionId"] != session_id:
            raise CommandError("WRONG_SESSION", "Evidence session does not match the task session.", stage="provider_result")

    def _require_plan(self, plan_id: str, task_id: str | None) -> None:
        plan = self.ledger.get_plan(plan_id)
        if task_id is None or plan["taskId"] != task_id:
            raise CommandError("AUTH_REQUIRED", "Provider plan does not belong to the command task.", stage="provider_result")

    def _require_job(
        self,
        job_id: str,
        operation: str,
        task_id: str | None,
        plan_id: str | None,
        request_id: str,
    ) -> None:
        job = self.ledger.get_job(job_id)
        if job["operation"] != operation or job["taskId"] != task_id or job["requestId"] != request_id:
            raise CommandError("AUTH_REQUIRED", "Provider job does not belong to the command request, operation, and task.", stage="provider_result")
        if plan_id is not None and job["planId"] != plan_id:
            raise CommandError("CONTRACT_MISMATCH", "Provider job and result planId differ.", stage="provider_result")
        if job["state"] not in {"queued", "running"}:
            self._mismatch("An accepted provider result must reference a non-terminal Host job.")

    def _validate_evidence_artifact_ids(self, values: Any, task_id: str | None) -> list[str]:
        if not isinstance(values, list) or len(values) > 128:
            self._mismatch("Evidence artifact IDs must be a bounded array.")
        normalized = []
        for value in values:
            if not isinstance(value, str) or not value:
                self._mismatch("Evidence artifactId must be a non-empty string.")
            metadata, stream = self.artifacts.open_verified(value)
            stream.close()
            stored = self.ledger.get_artifact(value)
            if task_id is not None and stored["taskId"] not in {None, task_id}:
                raise CommandError("AUTH_REQUIRED", "Evidence artifact belongs to another task.", stage="provider_result")
            normalized.append(metadata["artifactId"])
        return normalized

    def _exact_object(self, value: Any, keys: set[str], label: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != keys:
            self._mismatch(f"{label} fields do not match the evidence contract.")
        return dict(value)
