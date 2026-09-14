from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CommandError
from .validation import SHA256_RE


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
EDITOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class EditorJobEnvelope:
    job_id: str
    kind: str
    input_snapshot: str
    provider_id: str
    artifact_root: str
    requested_at_utc: str
    expires_at_utc: str
    payload_json: str

    def as_dict(self) -> dict[str, str]:
        return {
            "jobId": self.job_id,
            "kind": self.kind,
            "inputSnapshot": self.input_snapshot,
            "providerId": self.provider_id,
            "artifactRoot": self.artifact_root,
            "requestedAtUtc": self.requested_at_utc,
            "expiresAtUtc": self.expires_at_utc,
            "payloadJson": self.payload_json,
        }


@dataclass(frozen=True, slots=True)
class EditorJobTicket:
    job_id: str
    request_digest: str
    input_snapshot: str
    provider_id: str
    submitted: bool


class EditorJobTransport:
    """Durable, no-replay Host side of the Editor worker file transport."""

    def __init__(self, job_root: str | Path, artifact_root: str | Path) -> None:
        self._job_root = self._absolute_root(job_root, "job_root")
        self._artifact_root = self._absolute_root(artifact_root, "artifact_root")
        self._incoming = self._job_root / "incoming"
        self._processing = self._job_root / "processing"
        self._results = self._job_root / "results"
        for directory in (self._incoming, self._processing, self._results, self._artifact_root):
            directory.mkdir(parents=True, exist_ok=True)

    def enqueue(self, envelope: EditorJobEnvelope) -> EditorJobTicket:
        body = self._encode_request(envelope)
        digest = hashlib.sha256(body).hexdigest()
        ticket = EditorJobTicket(
            envelope.job_id,
            digest,
            envelope.input_snapshot,
            envelope.provider_id,
            False,
        )

        existing = self._find_existing_request(envelope.job_id)
        if existing is not None:
            self._require_same_request(existing, body)
            return ticket

        result_path = self._result_path(envelope.job_id)
        if result_path.exists():
            result = self._read_and_validate_result(result_path, ticket)
            if result["requestDigest"] != digest:
                self._input_changed(envelope.job_id)
            return ticket

        self._require_unexpired(envelope.expires_at_utc)
        destination = self._request_path(self._incoming, envelope.job_id)
        temporary = self._incoming / f".{envelope.job_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self._require_same_request(destination, body)
                return ticket
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        return EditorJobTicket(
            envelope.job_id,
            digest,
            envelope.input_snapshot,
            envelope.provider_id,
            True,
        )

    def poll_result(self, ticket: EditorJobTicket) -> dict[str, Any] | None:
        path = self._result_path(ticket.job_id)
        if not path.exists():
            return None
        return self._read_and_validate_result(path, ticket)

    def wait(
        self,
        ticket: EditorJobTicket,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, Any]:
        if timeout_seconds < 0 or poll_interval_seconds <= 0:
            self._contract_mismatch("Wait durations must be non-negative with a positive poll interval.")
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = self.poll_result(ticket)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError(
                    "TIMEOUT",
                    f"Editor job {ticket.job_id} has no durable result yet.",
                    stage="editor_wait",
                    runtime_changed=False,
                    recoverable=True,
                    details={"jobId": ticket.job_id, "requestDigest": ticket.request_digest},
                )
            time.sleep(min(poll_interval_seconds, remaining))

    def _encode_request(self, envelope: EditorJobEnvelope) -> bytes:
        if not isinstance(envelope, EditorJobEnvelope):
            self._contract_mismatch("Editor request must be an EditorJobEnvelope.")
        for name, value in (
            ("jobId", envelope.job_id),
            ("kind", envelope.kind),
            ("providerId", envelope.provider_id),
        ):
            if not isinstance(value, str) or not EDITOR_ID_RE.fullmatch(value):
                self._contract_mismatch(f"{name} is not a safe identifier.")
        if not isinstance(envelope.input_snapshot, str) or not 1 <= len(envelope.input_snapshot) <= 256:
            self._contract_mismatch("inputSnapshot must contain 1..256 characters.")
        requested = self._parse_time(envelope.requested_at_utc, "requestedAtUtc")
        expires = self._parse_time(envelope.expires_at_utc, "expiresAtUtc")
        if expires <= requested:
            self._contract_mismatch("expiresAtUtc must be later than requestedAtUtc.")

        request_artifact_root = self._absolute_root(envelope.artifact_root, "artifactRoot")
        if not self._is_contained(self._artifact_root, request_artifact_root):
            raise CommandError(
                "AUTH_REQUIRED",
                "Editor request artifactRoot is outside the configured artifact root.",
                stage="authorization",
                runtime_changed=False,
                recoverable=False,
            )
        if not isinstance(envelope.payload_json, str):
            self._contract_mismatch("payloadJson must be a JSON string.")
        try:
            payload = self._json_loads(envelope.payload_json)
        except (TypeError, ValueError) as exc:
            self._contract_mismatch(f"payloadJson is invalid JSON: {exc}.")
        if not isinstance(payload, dict):
            self._contract_mismatch("payloadJson must encode an object.")

        normalized = envelope.as_dict()
        normalized["artifactRoot"] = str(request_artifact_root)
        try:
            body = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._contract_mismatch(f"Editor request is not valid JSON data: {exc}.")
        if not 0 < len(body) <= MAX_REQUEST_BYTES:
            self._contract_mismatch(f"Editor request exceeds {MAX_REQUEST_BYTES} bytes.")
        return body

    def _find_existing_request(self, job_id: str) -> Path | None:
        incoming = self._request_path(self._incoming, job_id)
        processing = self._request_path(self._processing, job_id)
        found = [path for path in (incoming, processing) if path.exists()]
        if len(found) > 1:
            self._contract_mismatch(f"Editor job {job_id} exists in both incoming and processing state.")
        return found[0] if found else None

    def _require_same_request(self, path: Path, expected: bytes) -> None:
        actual = self._read_bounded(path, MAX_REQUEST_BYTES, "Editor request")
        if not actual or not hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest():
            self._input_changed(path.name.removesuffix(".request.json"))

    def _read_and_validate_result(
        self, path: Path, ticket: EditorJobTicket
    ) -> dict[str, Any]:
        raw = self._read_bounded(path, MAX_RESULT_BYTES, "Editor result")
        try:
            value = self._json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._contract_mismatch(f"Editor result is invalid JSON: {exc}.")
        if not isinstance(value, dict):
            self._contract_mismatch("Editor result must be an object.")
        expected_keys = {
            "jobId",
            "requestDigest",
            "inputSnapshot",
            "providerId",
            "attemptId",
            "status",
            "completedAtUtc",
            "resultJson",
            "error",
            "artifacts",
        }
        if set(value) != expected_keys:
            self._contract_mismatch("Editor result fields do not match the result contract.")

        for key, expected in (
            ("jobId", ticket.job_id),
            ("inputSnapshot", ticket.input_snapshot),
            ("providerId", ticket.provider_id),
        ):
            if value[key] != expected:
                self._input_changed(ticket.job_id)
        if value["requestDigest"] != ticket.request_digest:
            self._input_changed(ticket.job_id)
        if not isinstance(value["requestDigest"], str) or not SHA256_RE.fullmatch(value["requestDigest"]):
            self._contract_mismatch("requestDigest must be a lowercase SHA-256 value.")
        if not isinstance(value["attemptId"], str) or not EDITOR_ID_RE.fullmatch(value["attemptId"]):
            self._contract_mismatch("attemptId is not a safe identifier.")
        self._parse_time(value["completedAtUtc"], "completedAtUtc")

        status = value["status"]
        if status not in {"completed", "failed", "state_unknown"}:
            self._contract_mismatch("Editor result status is unsupported.")
        error = self._normalize_error(value["error"])
        if status == "completed":
            if error is not None:
                self._contract_mismatch("A completed Editor result cannot contain an error.")
            if not isinstance(value["resultJson"], str):
                self._contract_mismatch("A completed Editor result requires resultJson.")
            try:
                self._json_loads(value["resultJson"])
            except ValueError as exc:
                self._contract_mismatch(f"resultJson is invalid JSON: {exc}.")
        else:
            if error is None:
                self._contract_mismatch("A failed Editor result requires a structured error.")
            if status == "state_unknown" and error["code"] != "STATE_UNKNOWN":
                self._contract_mismatch("state_unknown requires a STATE_UNKNOWN error.")
            if value["resultJson"] is not None:
                self._contract_mismatch("A failed Editor result cannot contain resultJson.")
        value["error"] = error
        value["artifacts"] = self._validate_artifacts(value["artifacts"])
        return value

    def _normalize_error(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            self._contract_mismatch("Editor result error must be an object or null.")
        if not any(value.get(key) for key in ("code", "stage", "message")):
            return None
        expected = {
            "code",
            "stage",
            "message",
            "recoverable",
            "runtimeChangedKnown",
            "runtimeChanged",
        }
        if set(value) != expected:
            self._contract_mismatch("Editor result error fields do not match the error contract.")
        for key in ("code", "stage", "message"):
            if not isinstance(value[key], str) or not value[key]:
                self._contract_mismatch(f"Editor result error.{key} is required.")
        for key in ("recoverable", "runtimeChangedKnown", "runtimeChanged"):
            if type(value[key]) is not bool:
                self._contract_mismatch(f"Editor result error.{key} must be a boolean.")
        if not value["runtimeChangedKnown"] and value["runtimeChanged"]:
            self._contract_mismatch("runtimeChanged cannot be true when runtimeChangedKnown is false.")
        return value

    def _validate_artifacts(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 128:
            self._contract_mismatch("Editor result artifacts must contain at most 128 entries.")
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        keys = {"artifactId", "kind", "path", "sha256", "mediaType", "size"}
        for index, artifact in enumerate(value):
            if not isinstance(artifact, dict) or set(artifact) != keys:
                self._contract_mismatch(f"artifacts[{index}] fields do not match the artifact contract.")
            artifact_id = artifact["artifactId"]
            if not isinstance(artifact_id, str) or not EDITOR_ID_RE.fullmatch(artifact_id):
                self._contract_mismatch(f"artifacts[{index}].artifactId is invalid.")
            if artifact_id in seen:
                self._contract_mismatch("Editor result contains a duplicate artifactId.")
            seen.add(artifact_id)
            if not isinstance(artifact["kind"], str) or not artifact["kind"]:
                self._contract_mismatch(f"artifacts[{index}].kind is required.")
            if not isinstance(artifact["mediaType"], str):
                self._contract_mismatch(f"artifacts[{index}].mediaType must be a string.")
            if type(artifact["size"]) is not int or artifact["size"] < 0:
                self._contract_mismatch(f"artifacts[{index}].size must be non-negative.")
            if not isinstance(artifact["sha256"], str) or not SHA256_RE.fullmatch(artifact["sha256"]):
                self._contract_mismatch(f"artifacts[{index}].sha256 is invalid.")
            artifact_path = self._absolute_root(artifact["path"], f"artifacts[{index}].path")
            if not self._is_contained(self._artifact_root, artifact_path):
                raise CommandError(
                    "AUTH_REQUIRED",
                    "Editor result contains an artifact outside the configured root.",
                    stage="authorization",
                    runtime_changed=False,
                    recoverable=False,
                )
            if not artifact_path.is_file():
                self._contract_mismatch(f"artifacts[{index}] references a missing file.")
            actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if actual != artifact["sha256"] or artifact_path.stat().st_size != artifact["size"]:
                self._contract_mismatch(f"artifacts[{index}] hash or size does not match the sealed file.")
            copy = dict(artifact)
            copy["path"] = str(artifact_path)
            validated.append(copy)
        return validated

    @staticmethod
    def _absolute_root(value: Any, name: str) -> Path:
        if not isinstance(value, (str, Path)):
            raise CommandError(
                "CONTRACT_MISMATCH",
                f"{name} must be a string or Path.",
                stage="validation",
                runtime_changed=False,
                recoverable=True,
            )
        try:
            path = Path(value)
        except (TypeError, ValueError) as exc:
            raise CommandError(
                "CONTRACT_MISMATCH",
                f"{name} is not a valid path.",
                stage="validation",
                runtime_changed=False,
                recoverable=True,
            ) from exc
        if not path.is_absolute():
            raise CommandError(
                "CONTRACT_MISMATCH",
                f"{name} must be an absolute path.",
                stage="validation",
                runtime_changed=False,
                recoverable=True,
            )
        try:
            return path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CommandError(
                "CONTRACT_MISMATCH",
                f"{name} cannot be normalized as an absolute path.",
                stage="validation",
                runtime_changed=False,
                recoverable=True,
            ) from exc

    @staticmethod
    def _is_contained(root: Path, candidate: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _parse_time(value: Any, name: str) -> datetime:
        if not isinstance(value, str):
            EditorJobTransport._contract_mismatch(f"{name} must be an ISO-8601 timestamp.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            EditorJobTransport._contract_mismatch(f"{name} must be an ISO-8601 timestamp.")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            EditorJobTransport._contract_mismatch(f"{name} must include a UTC offset.")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _json_loads(value: str) -> Any:
        def reject_constant(constant: str) -> None:
            raise ValueError(f"non-finite JSON number {constant}")

        return json.loads(value, parse_constant=reject_constant)

    @staticmethod
    def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
        try:
            size = path.stat().st_size
            if not 0 < size <= maximum:
                EditorJobTransport._contract_mismatch(f"{label} size is outside the allowed bounds.")
            value = path.read_bytes()
            if not 0 < len(value) <= maximum:
                EditorJobTransport._contract_mismatch(f"{label} size changed outside the allowed bounds.")
            return value
        except FileNotFoundError:
            raise
        except OSError as exc:
            EditorJobTransport._contract_mismatch(f"{label} cannot be read: {exc}.")

    @staticmethod
    def _require_unexpired(value: str) -> None:
        if EditorJobTransport._parse_time(value, "expiresAtUtc") <= datetime.now(timezone.utc):
            raise CommandError(
                "CONFLICT",
                "Editor request has expired and was not durably submitted.",
                stage="editor_enqueue",
                runtime_changed=False,
                recoverable=True,
            )

    @staticmethod
    def _request_path(directory: Path, job_id: str) -> Path:
        return directory / f"{job_id}.request.json"

    def _result_path(self, job_id: str) -> Path:
        return self._results / f"{job_id}.result.json"

    @staticmethod
    def _input_changed(job_id: str) -> None:
        raise CommandError(
            "INPUT_CHANGED",
            f"Editor job id {job_id} is already bound to different durable input.",
            stage="editor_transport",
            runtime_changed=False,
            recoverable=False,
        )

    @staticmethod
    def _contract_mismatch(message: str) -> None:
        raise CommandError(
            "CONTRACT_MISMATCH",
            message,
            stage="editor_transport",
            runtime_changed=False,
            recoverable=True,
        )
