from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .artifacts import ArtifactStore
from .editor_transport import EditorJobEnvelope, EditorJobTransport
from .errors import CommandError
from .ledger import Ledger
from .native_compile_profile import _reject_reparse_path
from .result_policy import ProviderResultPolicy
from .validation import SHA256_RE


class SourceBindingResolver(Protocol):
    """Private adapters resolve authenticated, current runtime provenance."""

    def resolve(self, task: dict[str, Any], source_id: str, context: dict[str, Any]) -> dict[str, Any]: ...
    def validate(self, task: dict[str, Any], binding: dict[str, Any]) -> None: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class EditorSourceProvider:
    """One durable target asset save, with polling through the shared command service."""

    capability = "source"

    def __init__(self, ledger: Ledger, artifacts: ArtifactStore, transport: EditorJobTransport,
                 resolver: SourceBindingResolver, workspace_root: Path, provider_id: str):
        self.ledger, self.artifacts, self.transport, self.resolver = ledger, artifacts, transport, resolver
        self.workspace_root = Path(workspace_root).absolute()
        self.provider_id = provider_id
        self.policy = ProviderResultPolicy(ledger, artifacts)

    @property
    def is_verified(self) -> bool:
        version = getattr(self.resolver, "source_save_contract_version", None)
        return bool(getattr(self.resolver, "is_verified", False)) and type(version) is int and version == 1

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        task = self.ledger.get_task(command["taskId"])
        context = command["context"]
        if any(not context.get(key) for key in ("sessionId", "expectedLaunchId", "expectedRuntimeRevision")):
            self._fail("INVALID_REQUEST", "Source operations require the observed session, launch and runtime revision.")
        if context["sessionId"] != task["sessionId"]:
            self._fail("WRONG_SESSION", "Source command session differs from the task.")
        if operation == "source.locate":
            binding = self._binding(task, command["arguments"]["target"], context)
            return {"status": "completed", "runtimeChanged": False, "result": {"source": binding}, "facts": {}}
        if operation != "source.edit":
            self._fail("INVALID_REQUEST", "Source provider operation is unsupported.")
        edits = command["arguments"]["edits"]
        ids = {edit["sourceId"] for edit in edits}
        if len(ids) != 1:
            self._fail("INVALID_REQUEST", "One source.edit transaction may save exactly one source asset.")
        binding = self._binding(task, next(iter(ids)), context)
        if next(iter(ids)) != binding["sourceId"]:
            self._fail("STALE_TARGET", "Source edit id differs from the explicitly resolved source.")
        if binding["sourceHash"] != command["arguments"]["expectedSourceHash"]:
            self._fail("INPUT_CHANGED", "Source hash differs from the located asset.")
        properties = [edit["property"] for edit in edits]
        if len(set(properties)) != len(properties) or not set(properties) <= set(binding["properties"]):
            self._fail("AUTH_REQUIRED", "Property edits are duplicated or outside the resolved source scope.")
        edit_context = {"schema": "relay.liveloop.source-edit-context", "version": 1,
                        "bindingJson": _json(binding), "expectedSourceHash": binding["sourceHash"],
                        "edits": [{"propertyPath": edit["property"], "expectedValueJson": _json(edit["expectedValue"]),
                                   "replacementValueJson": _json(edit["newValue"])} for edit in edits]}
        payload = {"sourceGuid": binding["sourceGuid"], "localId": binding["localId"],
                   "propertyPath": edit_context["edits"][0]["propertyPath"],
                   "expectedValueJson": edit_context["edits"][0]["expectedValueJson"],
                   "replacementValueJson": edit_context["edits"][0]["replacementValueJson"],
                   "providerContextJson": _json(edit_context)}
        snapshot = "sha256:" + hashlib.sha256(_json(edit_context).encode("utf-8")).hexdigest()
        job = self.ledger.create_job({"requestId": command["requestId"], "operation": operation,
                                     "taskId": task["taskId"], "state": "queued", "stage": "source_queued", "runtimeChanged": False,
                                     "result": {"binding": binding, "payload": payload, "inputSnapshot": snapshot,
                                                "sourceSaved": None, "sourceChanged": None, "runtimeApplied": False}})
        envelope = EditorJobEnvelope(job["jobId"], "source.edit", snapshot, self.provider_id,
                                     str(self.transport.artifact_root), datetime.now(timezone.utc).isoformat(),
                                     (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), _json(payload))
        record = dict(job["result"])
        record["editorEnvelope"] = envelope.as_dict()
        self.ledger.update_job(job["jobId"], state="queued", stage="source_queued", runtime_changed=False, result=record)
        self.refresh_job(job["jobId"])
        # Enqueueing and even a fast local result never changes accepted into a saved assertion.
        return {"status": "accepted", "jobId": job["jobId"], "runtimeChanged": False,
                "result": {"job": self.ledger.get_job(job["jobId"])}, "facts": {}}

    def refresh_job(self, job_id: str) -> None:
        job = self.ledger.get_job(job_id)
        if job["operation"] != "source.edit" or job["state"] not in {"queued", "running"}:
            return
        record = dict(job.get("result") or {})
        try:
            if job["stage"] == "source_queued" and job["cancelRequested"]:
                self.ledger.update_job(job_id, state="cancelled", stage="source_cancelled", runtime_changed=False, result=record)
                return
            raw = record["editorEnvelope"]
            envelope = EditorJobEnvelope(raw["jobId"], raw["kind"], raw["inputSnapshot"], raw["providerId"],
                                         raw["artifactRoot"], raw["requestedAtUtc"], raw["expiresAtUtc"], raw["payloadJson"])
            if envelope.job_id != job_id or envelope.provider_id != self.provider_id or envelope.payload_json != _json(record["payload"]):
                self._fail("STATE_UNKNOWN", "Durable source job has a contradictory Editor envelope.")
            task = self.ledger.get_task(job["taskId"])
            self._check_binding(task, record["binding"], check_hash=job["stage"] == "source_queued" and not self.transport.has_dispatch_evidence(job_id))
            ticket = self.transport.enqueue(envelope, allow_submit=job["stage"] == "source_queued")
            record["requestDigest"] = ticket.request_digest
            self.ledger.update_job(job_id, state="running", stage="source_dispatched", runtime_changed=False, result=record)
            terminal = self.transport.poll_result(ticket)
            if terminal is None:
                return
            if terminal["status"] != "completed":
                error = terminal["error"]
                raise CommandError(error["code"], error["message"], stage=error["stage"], runtime_changed=False,
                                   recoverable=error["recoverable"], details={"automaticSourceReplayAllowed": False})
            receipt = json.loads(terminal["resultJson"])
            self._check_receipt(receipt, job, record, terminal["attemptId"])
            record["sourceSaveReceipt"] = receipt
            record["runtimeApplied"] = False
            metadata = []
            for artifact in terminal["artifacts"]:
                metadata.append(self.artifacts.register(artifact["path"], kind=artifact["kind"], expected_sha256=artifact["sha256"],
                                                        expected_size=artifact["size"], media_type=artifact["mediaType"], task_id=job["taskId"]))
            record["artifacts"] = metadata
            receipts = [item for item in metadata if item["kind"] == "source_save_receipt"]
            if len(receipts) != 1:
                self._fail("CONTRACT_MISMATCH", "Source result must carry one immutable save receipt artifact.")
            if receipts[0]["size"] > 1024 * 1024:
                self._fail("CONTRACT_MISMATCH", "Source save receipt exceeds its bounded JSON size.")
            _, stream = self.artifacts.open_verified(receipts[0]["artifactId"])
            with stream:
                if json.load(stream) != receipt:
                    self._fail("CONTRACT_MISMATCH", "Source result differs from its sealed receipt artifact.")
            if receipt["phase"] != "completed":
                record["sourceChanged"] = receipt["sourceChanged"] if receipt["sourceChangedKnown"] else None
                record["sourceSaved"] = False if receipt["phase"] == "failed" else None
                self._fail("STATE_UNKNOWN" if receipt["phase"] == "state_unknown" else "CONFLICT", receipt["errorMessage"])
            path = self._path(record["binding"])
            if self._hash(path) != receipt["afterHash"]:
                self._fail("INPUT_CHANGED", "Saved source no longer matches the receipt's persisted hash.")
            record["sourceChanged"] = receipt["sourceChanged"]
            record["sourceSaved"] = True
            normalized = self.policy.validate("source.edit", job["taskId"], {
                "status": "completed", "runtimeChanged": False, "facts": {"sourceSaved": True},
                "result": {"sourceEvidence": {"inputSnapshot": envelope.input_snapshot, "savedHash": receipt["afterHash"],
                                              "providerId": self.provider_id}}, "artifacts": metadata,
            }, request_id=job["requestId"])
            record["facts"] = normalized["facts"]
            self.ledger.update_job(job_id, state="completed", stage="source_saved", runtime_changed=False, result=record)
        except Exception as failure:
            error = failure if isinstance(failure, CommandError) else CommandError("STATE_UNKNOWN", "Source receipt cannot be safely reconciled.", stage="source_save", runtime_changed=False, recoverable=False)
            self.ledger.update_job(job_id, state="state_unknown" if error.code == "STATE_UNKNOWN" else "failed",
                                   stage=error.stage, runtime_changed=False, result=record, error=error.as_dict())

    def recover_pending(self) -> None:
        for job in self.ledger.list_active_jobs():
            if job["operation"] == "source.edit":
                self.refresh_job(job["jobId"])

    def _binding(self, task: dict[str, Any], source_id: str, context: dict[str, Any]) -> dict[str, Any]:
        binding = self.resolver.resolve(task, source_id, context)
        self._check_binding(task, binding, check_hash=True)
        if binding["launchId"] != context["expectedLaunchId"] or binding["runtimeRevision"] != context["expectedRuntimeRevision"]:
            self._fail("STALE_TARGET", "Resolved source provenance differs from the observed runtime.")
        return binding

    def _check_binding(self, task: dict[str, Any], binding: dict[str, Any], *, check_hash: bool) -> None:
        fields = {"taskId", "taskUpdatedAt", "sessionId", "launchId", "runtimeRevision", "targetId", "pageId", "ownerGeneration",
                  "sourceId", "sourceGuid", "localId", "assetPath", "sourceHash", "properties"}
        if not isinstance(binding, dict) or set(binding) != fields:
            self._fail("CONTRACT_MISMATCH", "Source resolver binding fields are invalid.")
        if binding["taskId"] != task["taskId"] or binding["sessionId"] != task["sessionId"] or binding["taskUpdatedAt"] != task["updatedAt"]:
            self._fail("STALE_TARGET", "Source resolver binding differs from the current task/session/scope.")
        if isinstance(task["target"], str) and binding["targetId"] != task["target"]:
            self._fail("STALE_TARGET", "Source provenance belongs to another task target.")
        if not isinstance(binding["sourceHash"], str) or not SHA256_RE.fullmatch(binding["sourceHash"]) or not isinstance(binding["sourceGuid"], str) or not re.fullmatch(r"[a-f0-9]{32}", binding["sourceGuid"]):
            self._fail("CONTRACT_MISMATCH", "Source hash or GUID is invalid.")
        for key in ("launchId", "runtimeRevision", "targetId", "pageId", "sourceId"):
            if not isinstance(binding[key], str) or not binding[key] or len(binding[key]) > 2000:
                self._fail("CONTRACT_MISMATCH", "Source provenance contains an invalid identifier.")
        if any(type(binding[key]) is not int or not 0 <= binding[key] <= 2**63 - 1 for key in ("localId", "ownerGeneration")):
            self._fail("CONTRACT_MISMATCH", "Source local id or generation is invalid.")
        if not isinstance(binding["properties"], list) or not binding["properties"] or len(binding["properties"]) > 128 or any(not isinstance(item, str) or not item or len(item) > 512 for item in binding["properties"]):
            self._fail("CONTRACT_MISMATCH", "Source property scope is invalid.")
        self.resolver.validate(task, binding)
        path = self._path(binding)
        meta = Path(str(path) + ".meta")
        _reject_reparse_path(self.workspace_root, meta)
        if not re.search(r"(?m)^guid: " + re.escape(binding["sourceGuid"]) + r"\s*$", meta.read_text(encoding="utf-8")):
            self._fail("INPUT_CHANGED", "Resolved asset GUID does not match the source meta file.")
        if check_hash and self._hash(path) != binding["sourceHash"]:
            self._fail("INPUT_CHANGED", "Resolved source hash differs from the persistent file.")

    def _path(self, binding: dict[str, Any]) -> Path:
        relative = PurePosixPath(binding["assetPath"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or ":" in str(relative) or not relative.parts or relative.parts[0] != "Assets" or relative.suffix.lower() not in {".prefab", ".asset", ".mat"}:
            self._fail("AUTH_REQUIRED", "Source save path must name one supported asset under Assets.")
        path = self.workspace_root.joinpath(*relative.parts)
        _reject_reparse_path(self.workspace_root, path)
        if not path.is_file():
            self._fail("NOT_FOUND", "Resolved source asset does not exist.")
        return path

    @staticmethod
    def _hash(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _check_receipt(self, receipt: Any, job: dict[str, Any], record: dict[str, Any], attempt_id: str) -> None:
        keys = {"schema", "version", "jobId", "attemptId", "requestDigest", "bindingJson", "phase", "beforeHash", "afterHash",
                "sourcePersisted", "sourceChangedKnown", "sourceChanged", "runtimeApplied", "rollbackAttempted", "rollbackSucceeded", "errorCode", "errorMessage"}
        if not isinstance(receipt, dict) or set(receipt) != keys or receipt["schema"] != "relay.liveloop.source-save-receipt" or type(receipt["version"]) is not int or receipt["version"] != 1:
            self._fail("CONTRACT_MISMATCH", "Source save receipt fields are invalid.")
        if receipt["jobId"] != job["jobId"] or receipt["attemptId"] != attempt_id or receipt["requestDigest"] != record["requestDigest"] or receipt["bindingJson"] != _json(record["binding"]) or receipt["beforeHash"] != record["binding"]["sourceHash"]:
            self._fail("STALE_TARGET", "Source receipt belongs to another job/task/launch/page/generation or source input.")
        if any(type(receipt[key]) is not bool for key in ("sourcePersisted", "sourceChangedKnown", "sourceChanged", "runtimeApplied", "rollbackAttempted", "rollbackSucceeded")) or receipt["runtimeApplied"]:
            self._fail("CONTRACT_MISMATCH", "Source receipt facts are invalid or claim Player application.")
        if receipt["phase"] not in {"completed", "failed", "state_unknown"}:
            self._fail("STATE_UNKNOWN", "Source receipt is not terminal; mutation is never replayed.")
        if receipt["phase"] == "completed" and (not receipt["sourcePersisted"] or not receipt["sourceChangedKnown"] or not isinstance(receipt["afterHash"], str) or not SHA256_RE.fullmatch(receipt["afterHash"]) or receipt["sourceChanged"] != (receipt["beforeHash"] != receipt["afterHash"])):
            self._fail("CONTRACT_MISMATCH", "A completed source receipt must prove saved state and exact change attribution.")
        if receipt["phase"] == "failed" and (receipt["sourcePersisted"] or not receipt["sourceChangedKnown"] or receipt["sourceChanged"]):
            self._fail("CONTRACT_MISMATCH", "A failed source receipt must prove no persisted source change.")
        if receipt["phase"] == "state_unknown" and (receipt["sourcePersisted"] or receipt["sourceChangedKnown"]):
            self._fail("CONTRACT_MISMATCH", "An unknown source receipt cannot assert saved/change facts.")

    @staticmethod
    def _fail(code: str, message: str) -> None:
        raise CommandError(code, message, stage="source_save", runtime_changed=False, recoverable=False,
                           details={"automaticSourceReplayAllowed": False})
