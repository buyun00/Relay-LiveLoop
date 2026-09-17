from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .editor_transport import EditorJobEnvelope
from .errors import CommandError, capability_unavailable
from .native_compile_profile import (
    NativeCompileProfile,
    NativeCompileProfileRegistry,
    native_compile_profile_digest,
    native_input_snapshot,
    verify_profile_baselines,
)
from .native_compile_receipt import (
    COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
    MAX_RECEIPT_BYTES,
    validate_compiler_input_coverage,
    validate_native_compile_input_receipt,
)
from .runtime_update_provider import (
    MANIFEST_KIND,
    MANIFEST_SCHEMA,
    PREPARATION_SCHEMA,
    MAX_MANIFEST_BYTES,
    MAX_RUNTIME_ARTIFACT_BYTES,
    PlayerRuntimeUpdateProvider,
    _parse_object,
    runtime_revision_after,
)
from .validation import ID_RE, SHA256_RE


ANALYSIS_SCHEMA = "relay.liveloop.native-compile-analysis"
ANALYSIS_ARTIFACT_KIND = "native-compile-analysis"
COMPILE_MANIFEST_ARTIFACT_KIND = "compile-manifest"
COMPILE_INPUT_RECEIPT_ARTIFACT_KIND = "compile-input-receipt"
EDITOR_PROVIDER_ID = "ozdqp-editor-v1"
EDITOR_ARTIFACT_KINDS = {
    COMPILE_MANIFEST_ARTIFACT_KIND: "native_compile_manifest",
    COMPILE_INPUT_RECEIPT_ARTIFACT_KIND: "native_compile_input_receipt",
    ANALYSIS_ARTIFACT_KIND: "native_compile_analysis",
    "managed-assembly": "native_compile_assembly",
    "managed-symbols": "native_compile_symbols",
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(char in value for char in "\r\n\0"):
        raise ValueError(f"{label} must be bounded canonical text")
    value.encode("utf-8", errors="strict")
    return value


def _id_for(job_id: str, purpose: str, source_id: str = "") -> str:
    material = "\n".join(("relay.liveloop.native-artifact/1", job_id, purpose, source_id)).encode("utf-8")
    return "native_" + hashlib.sha256(material).hexdigest()[:48]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class NativeSourcePreparationProvider:
    """Compile the current task source through the durable Editor worker and produce a Host-bound Native plan input."""

    provider_id = "source-snapshot-native-compile-preparation"

    def __init__(
        self,
        ledger: Any,
        artifacts: Any,
        editor_transport: Any,
        profiles: NativeCompileProfileRegistry,
        runtime_provider: PlayerRuntimeUpdateProvider,
        *,
        editor_provider_id: str = EDITOR_PROVIDER_ID,
    ) -> None:
        if ledger is None or not callable(getattr(ledger, "get_job", None)):
            raise ValueError("ledger must expose durable job reads and updates")
        if artifacts is None or not callable(getattr(artifacts, "register", None)):
            raise ValueError("artifacts must expose immutable registration")
        if editor_transport is None or not all(callable(getattr(editor_transport, name, None)) for name in ("enqueue", "wait", "poll_result")):
            raise ValueError("editor_transport must expose durable enqueue/wait/poll operations")
        if not isinstance(profiles, NativeCompileProfileRegistry):
            raise ValueError("profiles must be a NativeCompileProfileRegistry")
        self._ledger = ledger
        self._artifacts = artifacts
        self._editor = editor_transport
        self._profiles = profiles
        self._runtime = runtime_provider
        self._editor_provider_id = _text(editor_provider_id, "editor_provider_id", 128)

    @property
    def is_verified(self) -> bool:
        return self._runtime.is_verified and bool(self._profiles)

    @property
    def unverified_reason(self) -> str | None:
        return self._runtime.unverified_reason

    def probe_capability(self, capability: str | None = None) -> bool:
        del capability
        return self.is_verified

    def profile_for_task(self, task: dict[str, Any]) -> NativeCompileProfile:
        return self._profiles.for_task(task)

    def current_profile_digest(self, task: dict[str, Any]) -> str:
        return native_compile_profile_digest(self.profile_for_task(task))

    def current_input_snapshot(self, task: dict[str, Any]) -> str:
        profile = self.profile_for_task(task)
        try:
            return native_input_snapshot(profile)
        except (OSError, UnicodeError, ValueError, OverflowError) as exc:
            raise CommandError(
                "INPUT_CHANGED",
                "Configured source inputs cannot be snapshotted safely.",
                stage="source_snapshot",
                runtime_changed=False,
                recoverable=False,
                details={"failureType": type(exc).__name__},
            ) from exc

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        job_id = request.get("jobId")
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            raise CommandError("CONTRACT_MISMATCH", "Prepare request is missing its durable Host job identity.", stage="prepare", runtime_changed=False, recoverable=False)
        profile = self.profile_for_task(task)
        binding = self._make_binding(task, request, profile)
        existing = self._ledger.get_job(job_id)
        saved_binding = (existing.get("result") or {}).get("prepareBinding")
        if saved_binding is not None and saved_binding != binding:
            raise CommandError("INPUT_CHANGED", "Prepare job identity is already bound to different task/source/runtime inputs.", stage="prepare", runtime_changed=False, recoverable=False)
        self._save_job(job_id, "prepare_running", {"prepareBinding": binding})
        verify_profile_baselines(profile)
        current_snapshot = self.current_input_snapshot(task)
        if current_snapshot != binding["inputSnapshot"]:
            raise CommandError("INPUT_CHANGED", "Source inputs changed before the Editor compile request was submitted.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        envelope_value = (self._ledger.get_job(job_id).get("result") or {}).get("editorRequest")
        if envelope_value is None:
            envelope = self._make_envelope(job_id, task, profile, binding)
            self._save_job(
                job_id,
                "prepare_editor_dispatching",
                {"editorRequest": envelope.as_dict(), "editorDispatchState": "dispatching"},
            )
            allow_submit = True
        else:
            envelope = self._envelope_from_dict(envelope_value)
            self._verify_envelope_binding(envelope, job_id, task, profile, binding)
            # An already-persisted envelope may have crossed the filesystem
            # boundary before a Host crash. Reconciliation may only inspect it.
            allow_submit = False
        ticket = self._editor.enqueue(envelope, allow_submit=allow_submit)
        self._save_job(
            job_id,
            "prepare_editor_wait",
            {
                "editorRequestDigest": ticket.request_digest,
                "editorSubmitted": ticket.submitted,
                "editorDispatchState": "submitted",
            },
        )
        result = self._editor.wait(ticket, timeout_seconds=profile.timeout_seconds)
        return self._finish_editor_result(job_id, task, profile, binding, result, ticket.request_digest)

    def reconcile_prepare(self, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
        job_id = job.get("jobId")
        result_record = job.get("result") if isinstance(job.get("result"), dict) else {}
        binding = result_record.get("prepareBinding")
        if not isinstance(job_id, str) or not isinstance(binding, dict):
            return None
        profile = self.profile_for_task(task)
        self._verify_binding(task, profile, binding, job_id=job_id)
        saved = result_record.get("prepareProviderResult")
        if isinstance(saved, dict):
            return self._verify_saved_provider_result(job_id, task, profile, binding, saved)
        raw_envelope = result_record.get("editorRequest")
        if not isinstance(raw_envelope, dict):
            # The provider persists the exact envelope before enqueue. Without it, there is no
            # safe identity with which to reconcile a possibly dispatched compile request.
            return None
        envelope = self._envelope_from_dict(raw_envelope)
        self._verify_envelope_binding(envelope, job_id, task, profile, binding)
        ticket = self._editor.enqueue(envelope, allow_submit=False)
        saved_digest = result_record.get("editorRequestDigest")
        if saved_digest is not None and saved_digest != ticket.request_digest:
            raise CommandError("INPUT_CHANGED", "Recovered Editor request digest differs from the durable Host binding.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        recovered_result = self._editor.poll_result(ticket)
        if recovered_result is None:
            return None
        return self._finish_editor_result(job_id, task, profile, binding, recovered_result, ticket.request_digest)

    def _make_binding(self, task: dict[str, Any], request: dict[str, Any], profile: NativeCompileProfile) -> dict[str, Any]:
        try:
            snapshot = request["inputSnapshot"]
            task_updated_at = request["taskUpdatedAt"]
            runtime_state = request["runtimeState"]
            if not isinstance(runtime_state, dict) or set(runtime_state) != {
                "sessionId", "runtimeRevision", "moduleGeneration", "resourceRelease", "viewGeneration"
            }:
                raise ValueError("runtimeState does not match the authenticated observation contract")
            if (
                task.get("taskId") != request.get("taskId")
                or task.get("sessionId") != request.get("sessionId")
                or task.get("updatedAt") != task_updated_at
                or request.get("profileId") != profile.profile_id
                or request.get("profileDigest") != native_compile_profile_digest(profile)
                or snapshot != self.current_input_snapshot(task)
                or request.get("expectedRuntimeRevision") != runtime_state.get("runtimeRevision")
                or runtime_state.get("sessionId") != task.get("sessionId")
                or not isinstance(runtime_state.get("runtimeRevision"), str)
                or not runtime_state["runtimeRevision"]
                or type(runtime_state.get("moduleGeneration")) is not int
                or runtime_state["moduleGeneration"] < 0
                or not isinstance(runtime_state.get("resourceRelease"), str)
                or not runtime_state["resourceRelease"]
                or type(runtime_state.get("viewGeneration")) is not int
                or runtime_state["viewGeneration"] < 0
            ):
                raise ValueError("prepare binding does not match the current task, source, or observed runtime")
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError("INPUT_CHANGED", "Prepare binding changed before compile submission.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        if not isinstance(snapshot, str) or not snapshot or not isinstance(task_updated_at, str) or not task_updated_at:
            raise CommandError("CONTRACT_MISMATCH", "Prepare binding fields are incomplete.", stage="prepare", runtime_changed=False, recoverable=False)
        if not isinstance(request.get("jobId"), str) or not ID_RE.fullmatch(request["jobId"]):
            raise CommandError("CONTRACT_MISMATCH", "Prepare binding jobId is invalid.", stage="prepare", runtime_changed=False, recoverable=False)
        return {
            "schema": "relay.liveloop.native-prepare-binding",
            "version": 2,
            "providerId": self.provider_id,
            "jobId": request["jobId"],
            "profileId": profile.profile_id,
            "profileDigest": native_compile_profile_digest(profile),
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "launchId": self._runtime._launch_id,
            "taskUpdatedAt": task_updated_at,
            "inputSnapshot": snapshot,
            "runtimeState": dict(runtime_state),
        }

    def _verify_binding(
        self,
        task: dict[str, Any],
        profile: NativeCompileProfile,
        binding: dict[str, Any],
        *,
        job_id: str,
    ) -> None:
        runtime_state = binding.get("runtimeState")
        if (
            set(binding) != {
                "schema", "version", "providerId", "jobId", "profileId", "profileDigest", "taskId", "sessionId",
                "launchId", "taskUpdatedAt", "inputSnapshot", "runtimeState",
            }
            or binding.get("schema") != "relay.liveloop.native-prepare-binding"
            or binding.get("version") != 2
            or binding.get("providerId") != self.provider_id
            or binding.get("jobId") != job_id
            or binding.get("profileId") != profile.profile_id
            or binding.get("profileDigest") != native_compile_profile_digest(profile)
            or binding.get("taskId") != task.get("taskId")
            or binding.get("sessionId") != task.get("sessionId")
            or binding.get("launchId") != self._runtime._launch_id
            or binding.get("taskUpdatedAt") != task.get("updatedAt")
            or binding.get("inputSnapshot") != self.current_input_snapshot(task)
            or not isinstance(runtime_state, dict)
            or set(runtime_state) != {"sessionId", "runtimeRevision", "moduleGeneration", "resourceRelease", "viewGeneration"}
            or runtime_state.get("sessionId") != task.get("sessionId")
            or not isinstance(runtime_state.get("runtimeRevision"), str)
            or not runtime_state["runtimeRevision"]
            or type(runtime_state.get("moduleGeneration")) is not int
            or runtime_state["moduleGeneration"] < 0
            or not isinstance(runtime_state.get("resourceRelease"), str)
            or not runtime_state["resourceRelease"]
            or type(runtime_state.get("viewGeneration")) is not int
            or runtime_state["viewGeneration"] < 0
        ):
            raise CommandError("INPUT_CHANGED", "Recovered prepare binding no longer matches task/session/source inputs.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)

    def _make_envelope(self, job_id: str, task: dict[str, Any], profile: NativeCompileProfile, binding: dict[str, Any]) -> EditorJobEnvelope:
        context = {
            "schema": "relay.liveloop.native-compile-context",
            "version": 3,
            "profileId": profile.profile_id,
            "profileDigest": binding["profileDigest"],
            "projectRoot": profile.project_root.as_posix(),
            "unityRoot": profile.unity_root.as_posix(),
            "unityVersion": profile.unity_version,
            "buildTarget": profile.build_target,
            "jobId": job_id,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "launchId": binding["launchId"],
            "inputSnapshot": binding["inputSnapshot"],
            "buildTargetGroup": profile.build_target_group,
            "developmentBuild": profile.development_build,
            "subtarget": profile.subtarget,
            "extraScriptingDefines": list(profile.extra_scripting_defines),
            "moduleId": profile.module_id,
            "dependencyClosure": list(profile.dependency_closure),
            "entryAssemblyName": profile.entry_assembly_name,
            "assemblies": profile.context_assemblies(),
        }
        payload = {
            "buildTarget": profile.build_target,
            "configuration": profile.configuration,
            "defines": list(profile.defines),
            "references": list(profile.references),
            "sourceInputs": list(profile.source_inputs),
            "providerContextJson": _json_text(context),
        }
        requested_at = datetime.now(timezone.utc)
        return EditorJobEnvelope(
            job_id=job_id,
            kind="compile",
            input_snapshot=binding["inputSnapshot"],
            provider_id=self._editor_provider_id,
            artifact_root=str(self._editor.artifact_root),
            requested_at_utc=_timestamp(requested_at),
            expires_at_utc=_timestamp(requested_at + timedelta(seconds=max(300, profile.timeout_seconds + 60))),
            payload_json=_json_text(payload),
        )

    @staticmethod
    def _envelope_from_dict(value: dict[str, Any]) -> EditorJobEnvelope:
        required = {"jobId", "kind", "inputSnapshot", "providerId", "artifactRoot", "requestedAtUtc", "expiresAtUtc", "payloadJson"}
        if set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "Durable Editor request fields do not match the envelope contract.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        return EditorJobEnvelope(
            job_id=value["jobId"],
            kind=value["kind"],
            input_snapshot=value["inputSnapshot"],
            provider_id=value["providerId"],
            artifact_root=value["artifactRoot"],
            requested_at_utc=value["requestedAtUtc"],
            expires_at_utc=value["expiresAtUtc"],
            payload_json=value["payloadJson"],
        )

    def _verify_envelope_binding(
        self,
        envelope: EditorJobEnvelope,
        job_id: str,
        task: dict[str, Any],
        profile: NativeCompileProfile,
        binding: dict[str, Any],
    ) -> None:
        try:
            payload = _parse_object(envelope.payload_json.encode("utf-8"), "Durable Editor payload")
            context_raw = payload.get("providerContextJson")
            if not isinstance(context_raw, str):
                raise ValueError("providerContextJson must be a JSON string")
            context = _parse_object(context_raw.encode("utf-8"), "Durable native compile context")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Durable Editor payload cannot be reconciled.", stage="prepare_reconcile", runtime_changed=False, recoverable=False) from exc
        expected_context = {
            "schema": "relay.liveloop.native-compile-context",
            "version": 3,
            "profileId": profile.profile_id,
            "profileDigest": binding["profileDigest"],
            "projectRoot": profile.project_root.as_posix(),
            "unityRoot": profile.unity_root.as_posix(),
            "unityVersion": profile.unity_version,
            "buildTarget": profile.build_target,
            "jobId": job_id,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "launchId": binding["launchId"],
            "inputSnapshot": binding["inputSnapshot"],
            "buildTargetGroup": profile.build_target_group,
            "developmentBuild": profile.development_build,
            "subtarget": profile.subtarget,
            "extraScriptingDefines": list(profile.extra_scripting_defines),
            "moduleId": profile.module_id,
            "dependencyClosure": list(profile.dependency_closure),
            "entryAssemblyName": profile.entry_assembly_name,
            "assemblies": profile.context_assemblies(),
        }
        expected_payload = {
            "buildTarget": profile.build_target,
            "configuration": profile.configuration,
            "defines": list(profile.defines),
            "references": list(profile.references),
            "sourceInputs": list(profile.source_inputs),
            "providerContextJson": context_raw,
        }
        if (
            envelope.job_id != job_id
            or envelope.kind != "compile"
            or envelope.provider_id != self._editor_provider_id
            or envelope.input_snapshot != binding["inputSnapshot"]
            or Path(envelope.artifact_root).resolve() != self._editor.artifact_root.resolve()
            or set(payload) != set(expected_payload)
            or any(payload.get(key) != value for key, value in expected_payload.items())
            or context != expected_context
        ):
            raise CommandError("INPUT_CHANGED", "Durable Editor request is bound to another task/profile/session/compile input.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)

    def _finish_editor_result(
        self,
        job_id: str,
        task: dict[str, Any],
        profile: NativeCompileProfile,
        binding: dict[str, Any],
        editor_result: dict[str, Any],
        request_digest: str,
    ) -> dict[str, Any]:
        self._verify_binding(task, profile, binding, job_id=job_id)
        verify_profile_baselines(profile)
        if editor_result.get("status") != "completed":
            error = editor_result.get("error") or {}
            code = "STATE_UNKNOWN" if editor_result.get("status") == "state_unknown" else str(error.get("code") or "RESOURCE_BUILD_FAILED")
            raise CommandError(
                code,
                str(error.get("message") or "Editor compile job did not complete successfully."),
                stage=str(error.get("stage") or "editor_compile"),
                runtime_changed=False if code != "STATE_UNKNOWN" else None,
                recoverable=error.get("recoverable") is True,
                details={"editorJobId": job_id, "automaticCompileReplayAllowed": False},
            )
        try:
            analysis = _parse_object(editor_result["resultJson"].encode("utf-8"), "Native compile analysis")
        except (KeyError, AttributeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Completed Editor result has no valid native compile analysis.", stage="editor_compile", runtime_changed=False, recoverable=False) from exc
        state = binding["runtimeState"]
        current_state = self._runtime.observe_task_state(task)
        if current_state != state:
            raise CommandError("STALE_TARGET", "Authenticated Player state changed while Editor compiled the candidate.", stage="prepare", runtime_changed=False, recoverable=False)
        current_snapshot = self.current_input_snapshot(task)
        if current_snapshot != binding["inputSnapshot"]:
            raise CommandError("INPUT_CHANGED", "Source inputs changed while Editor compiled the candidate.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        editor_artifacts = editor_result.get("artifacts")
        if not isinstance(editor_artifacts, list):
            raise CommandError("CONTRACT_MISMATCH", "Editor result artifacts are malformed.", stage="editor_compile", runtime_changed=False, recoverable=False)
        by_editor_id = {item.get("artifactId"): item for item in editor_artifacts if isinstance(item, dict)}
        if len(by_editor_id) != len(editor_artifacts):
            raise CommandError("CONTRACT_MISMATCH", "Editor result contains duplicate or malformed artifact identities.", stage="editor_compile", runtime_changed=False, recoverable=False)
        registered_by_editor_id: dict[str, dict[str, Any]] = {}
        for item in editor_artifacts:
            editor_id = item.get("artifactId")
            host_kind = EDITOR_ARTIFACT_KINDS.get(item.get("kind"))
            if not isinstance(editor_id, str) or host_kind is None:
                raise CommandError("CONTRACT_MISMATCH", "Editor result contains an unsupported candidate artifact kind.", stage="editor_compile", runtime_changed=False, recoverable=False)
            registered_by_editor_id[editor_id] = self._artifacts.register(
                item["path"],
                kind=host_kind,
                expected_sha256=item["sha256"],
                expected_size=item["size"],
                media_type=item["mediaType"],
                artifact_id=_id_for(job_id, "editor", editor_id),
                task_id=task["taskId"],
                job_id=job_id,
            )
        analysis_document = analysis
        analysis = self._validate_analysis(analysis, profile, task, binding, by_editor_id)
        analysis_meta = registered_by_editor_id[analysis["analysisArtifactId"]]
        compile_manifest_meta = registered_by_editor_id[analysis["compileManifestArtifactId"]]
        receipt_meta = registered_by_editor_id[analysis["compileInputReceiptArtifactId"]]
        raw_analysis = self._read_artifact(analysis_meta["artifactId"], MAX_MANIFEST_BYTES)
        if _parse_object(raw_analysis, "Sealed native compile analysis") != analysis_document:
            raise CommandError("INPUT_CHANGED", "Editor result JSON differs from its sealed analysis artifact.", stage="editor_compile", runtime_changed=False, recoverable=False)
        verified_receipt = self._verify_receipt_artifact(
            receipt_meta["artifactId"],
            analysis["compileInputReceiptSha256"],
            profile,
            job_id=job_id,
            task_id=task["taskId"],
            input_snapshot=binding["inputSnapshot"],
            expected_output_files=[
                {
                    "assemblyName": item["assemblyName"],
                    "extension": item["extension"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                }
                for item in analysis["files"]
            ],
        )
        route = self._choose_route(profile, analysis)
        native_manifest, runtime_payload_ids = self._build_runtime_manifest(
            job_id, task, profile, binding, analysis, route, registered_by_editor_id
        )
        manifest_bytes = _json_text(native_manifest).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Generated runtime manifest exceeds 1 MiB.", stage="prepare", runtime_changed=False, recoverable=False)
        manifest_path = self._persist_generated_manifest(job_id, manifest_bytes)
        runtime_manifest_meta = self._artifacts.register(
            manifest_path,
            kind=MANIFEST_KIND,
            expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_size=len(manifest_bytes),
            media_type="application/json",
            artifact_id=_id_for(job_id, "runtime-manifest"),
            task_id=task["taskId"],
            job_id=job_id,
        )
        descriptor = {
            "schema": PREPARATION_SCHEMA,
            "version": 1,
            "inputSnapshot": binding["inputSnapshot"],
            "manifestArtifactId": runtime_manifest_meta["artifactId"],
            "artifactIds": [runtime_manifest_meta["artifactId"], *runtime_payload_ids],
        }
        normalized, kinds = self._runtime.prepare_manifest(task, descriptor, native_manifest, state)
        artifacts = []
        for artifact_id in descriptor["artifactIds"]:
            metadata, stream = self._open_task_artifact(task["taskId"], artifact_id)
            stream.close()
            artifacts.append(metadata)
        receipt_metadata, receipt_stream = self._open_task_artifact(task["taskId"], receipt_meta["artifactId"])
        receipt_stream.close()
        artifacts.append(receipt_metadata)
        if not set(kinds).issubset({item["kind"] for item in artifacts}):
            raise CommandError("RESOURCE_BUILD_FAILED", "Generated candidate is missing a required Native payload kind.", stage="prepare", runtime_changed=False)
        current_snapshot = self.current_input_snapshot(task)
        if current_snapshot != binding["inputSnapshot"]:
            raise CommandError("INPUT_CHANGED", "Source inputs changed before candidate plan finalization.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        prepared = {
            "inputSnapshot": binding["inputSnapshot"],
            "profileDigest": binding["profileDigest"],
            "expectedRuntimeRevision": state["runtimeRevision"],
            "expectedRuntimeRevisionAfter": normalized["expectedRuntimeRevisionAfter"],
            "route": normalized["route"],
            "requiredImpact": normalized["requiredImpact"],
            "artifacts": artifacts,
            "requiredArtifactKinds": sorted(set(kinds)),
            "affectedModules": normalized["affectedModules"],
            "affectedViews": normalized["affectedViews"],
            "targetGenerations": normalized["targetGenerations"],
            "targetGenerationsAfter": normalized["targetGenerationsAfter"],
            "approvalRequired": normalized["approvalRequired"],
            "prepareComplete": True,
            "preparationEvidence": {
                "providerId": self.provider_id,
                "profileId": profile.profile_id,
                "profileDigest": binding["profileDigest"],
                "editorJobId": job_id,
                "editorRequestDigest": request_digest,
                "analysisArtifactId": analysis_meta["artifactId"],
                "compileInputReceiptArtifactId": receipt_meta["artifactId"],
                "compileInputReceiptSha256": receipt_meta["sha256"],
                "compilerInputCoverage": COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
                "compilerInputLimitations": list(verified_receipt["limitations"]),
                "compileManifestArtifactId": compile_manifest_meta["artifactId"],
                "runtimeManifestArtifactId": runtime_manifest_meta["artifactId"],
                "editorCandidateArtifactIds": [registered_by_editor_id[item["artifactId"]]["artifactId"] for item in editor_artifacts],
                "assemblyDiffs": [
                    {
                        "name": item["name"],
                        "baselineSha256": item["baselineSha256"],
                        "changed": not item["structureEqual"] or bool(item["changedMethods"]),
                    }
                    for item in analysis["assemblies"]
                ],
            },
        }
        self._save_job(job_id, "prepare_candidate_ready", {"prepareProviderResult": prepared})
        return prepared

    def _validate_analysis(
        self,
        analysis: dict[str, Any],
        profile: NativeCompileProfile,
        task: dict[str, Any],
        binding: dict[str, Any],
        editor_artifacts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        required = {
            "schema", "version", "profileId", "profileDigest", "taskId", "sessionId", "launchId", "jobId", "inputSnapshot",
            "analysisArtifactId", "compileManifestArtifactId", "compileInputReceiptArtifactId",
            "compileInputReceiptSha256", "files", "assemblies",
        }
        if set(analysis) != required or analysis.get("schema") != ANALYSIS_SCHEMA or type(analysis.get("version")) is not int or analysis["version"] != 3:
            raise CommandError("CONTRACT_MISMATCH", "Native compile analysis fields/schema are unsupported.", stage="editor_compile", runtime_changed=False, recoverable=False)
        for key, expected in (
            ("profileId", profile.profile_id),
            ("profileDigest", binding["profileDigest"]),
            ("taskId", task["taskId"]),
            ("sessionId", task["sessionId"]),
            ("launchId", binding["launchId"]),
            ("jobId", binding["jobId"]),
            ("inputSnapshot", binding["inputSnapshot"]),
        ):
            if analysis.get(key) != expected:
                raise CommandError("WRONG_SESSION", f"Native compile analysis {key} differs from the durable prepare binding.", stage="editor_compile", runtime_changed=False, recoverable=False)
        for field, kind in (
            ("analysisArtifactId", ANALYSIS_ARTIFACT_KIND),
            ("compileManifestArtifactId", COMPILE_MANIFEST_ARTIFACT_KIND),
            ("compileInputReceiptArtifactId", COMPILE_INPUT_RECEIPT_ARTIFACT_KIND),
        ):
            artifact = editor_artifacts.get(analysis.get(field))
            if artifact is None or artifact.get("kind") != kind:
                raise CommandError("CONTRACT_MISMATCH", f"Native compile analysis {field} is not bound to its sealed Editor artifact.", stage="editor_compile", runtime_changed=False, recoverable=False)
        receipt_artifact = editor_artifacts[analysis["compileInputReceiptArtifactId"]]
        if (
            not isinstance(analysis["compileInputReceiptSha256"], str)
            or not SHA256_RE.fullmatch(analysis["compileInputReceiptSha256"])
            or receipt_artifact.get("sha256") != analysis["compileInputReceiptSha256"]
        ):
            raise CommandError("INPUT_CHANGED", "Native compile analysis does not bind the exact input-receipt artifact hash.", stage="editor_compile", runtime_changed=False, recoverable=False)
        raw_files = analysis["files"]
        raw_assemblies = analysis["assemblies"]
        if not isinstance(raw_files, list) or not isinstance(raw_assemblies, list):
            raise CommandError("CONTRACT_MISMATCH", "Native compile file/assembly analysis is malformed.", stage="editor_compile", runtime_changed=False, recoverable=False)
        if len(raw_files) > 126 or len(raw_assemblies) != len(profile.assemblies):
            raise CommandError("CONTRACT_MISMATCH", "Native compile output exceeds the supported complete closure.", stage="editor_compile", runtime_changed=False, recoverable=False)
        files_by_assembly: dict[str, dict[str, dict[str, Any]]] = {}
        for index, raw in enumerate(raw_files):
            if not isinstance(raw, dict) or set(raw) != {"artifactId", "assemblyName", "extension", "sha256", "size"}:
                raise CommandError("CONTRACT_MISMATCH", f"Native compile files[{index}] fields are invalid.", stage="editor_compile", runtime_changed=False, recoverable=False)
            artifact_id = raw["artifactId"]
            name = raw["assemblyName"]
            extension = raw["extension"]
            artifact = editor_artifacts.get(artifact_id)
            expected_kind = "managed-assembly" if extension == ".dll" else "managed-symbols" if extension == ".pdb" else None
            if (
                not isinstance(artifact_id, str)
                or not isinstance(name, str)
                or extension not in {".dll", ".pdb"}
                or not isinstance(raw["sha256"], str)
                or not SHA256_RE.fullmatch(raw["sha256"])
                or type(raw["size"]) is not int
                or raw["size"] <= 0
                or artifact is None
                or artifact.get("kind") != expected_kind
                or artifact.get("sha256") != raw["sha256"]
                or artifact.get("size") != raw["size"]
            ):
                raise CommandError("CONTRACT_MISMATCH", "Native compile file does not match a sealed Editor artifact.", stage="editor_compile", runtime_changed=False, recoverable=False)
            files_for_assembly = files_by_assembly.setdefault(name, {})
            if extension in files_for_assembly:
                raise CommandError("CONTRACT_MISMATCH", "Native compile analysis repeats an assembly file.", stage="editor_compile", runtime_changed=False, recoverable=False)
            files_for_assembly[extension] = raw
        if set(files_by_assembly) != {item.name for item in profile.assemblies}:
            raise CommandError("RESOURCE_BUILD_FAILED", "Editor output does not cover the configured full assembly closure.", stage="editor_compile", runtime_changed=False, recoverable=False)
        if any(".dll" not in files_by_assembly[item.name] for item in profile.assemblies):
            raise CommandError("RESOURCE_BUILD_FAILED", "Editor output is missing a required compiled DLL.", stage="editor_compile", runtime_changed=False, recoverable=False)
        expected_editor_artifact_ids = {
            analysis["analysisArtifactId"],
            analysis["compileManifestArtifactId"],
            analysis["compileInputReceiptArtifactId"],
        } | {raw["artifactId"] for raw in raw_files}
        if set(editor_artifacts) != expected_editor_artifact_ids:
            raise CommandError("CONTRACT_MISMATCH", "Editor artifact set contains unbound or omitted candidate output.", stage="editor_compile", runtime_changed=False, recoverable=False)
        if sum(raw["size"] for raw in raw_files) > MAX_RUNTIME_ARTIFACT_BYTES:
            raise CommandError("RESOURCE_BUILD_FAILED", "Compiled module closure exceeds the frozen 8 MiB runtime payload bound.", stage="editor_compile", runtime_changed=False, recoverable=False)

        expected_assemblies = {item.name: item for item in profile.assemblies}
        normalized_assemblies = []
        seen_names: set[str] = set()
        for index, raw in enumerate(raw_assemblies):
            if not isinstance(raw, dict) or set(raw) != {"name", "moduleId", "dependencies", "baselineSha256", "structureEqual", "changedMethods"}:
                raise CommandError("CONTRACT_MISMATCH", f"Native compile assemblies[{index}] fields are invalid.", stage="editor_compile", runtime_changed=False, recoverable=False)
            name = raw.get("name")
            expected = expected_assemblies.get(name)
            if expected is None or name in seen_names:
                raise CommandError("CONTRACT_MISMATCH", "Native compile analysis has an unknown or duplicate assembly.", stage="editor_compile", runtime_changed=False, recoverable=False)
            seen_names.add(name)
            methods = raw.get("changedMethods")
            if not isinstance(methods, list) or len(methods) > 100000 or type(raw.get("structureEqual")) is not bool:
                raise CommandError("CONTRACT_MISMATCH", "Native compile method/structure comparison is malformed.", stage="editor_compile", runtime_changed=False, recoverable=False)
            normalized_methods = []
            seen_methods: set[tuple[str, str]] = set()
            for method in methods:
                if not isinstance(method, dict) or set(method) != {"typeName", "signature"}:
                    raise CommandError("CONTRACT_MISMATCH", "Native changed method signature fields are invalid.", stage="editor_compile", runtime_changed=False, recoverable=False)
                type_name = _text(method["typeName"], "changed method typeName", 512)
                signature = _text(method["signature"], "changed method signature", 2048)
                pair = (type_name, signature)
                if pair in seen_methods:
                    raise CommandError("CONTRACT_MISMATCH", "Native compile analysis duplicates a changed method.", stage="editor_compile", runtime_changed=False, recoverable=False)
                seen_methods.add(pair)
                normalized_methods.append({"typeName": type_name, "signature": signature})
            if (
                raw.get("moduleId") != expected.module_id
                or raw.get("dependencies") != list(expected.dependencies)
                or raw.get("baselineSha256") != expected.baseline_sha256
            ):
                raise CommandError("INPUT_CHANGED", "Editor diff does not match the server-pinned baseline/profile closure.", stage="editor_compile", runtime_changed=False, recoverable=False)
            if not raw["structureEqual"] and normalized_methods:
                # Once metadata changes, method-body signatures are not sufficient to authorize hotfix.
                normalized_methods = []
            normalized_assemblies.append(
                {
                    "name": name,
                    "moduleId": expected.module_id,
                    "dependencies": list(expected.dependencies),
                    "baselineSha256": expected.baseline_sha256,
                    "structureEqual": raw["structureEqual"],
                    "changedMethods": normalized_methods,
                }
            )
        return {**analysis, "filesByAssembly": files_by_assembly, "assemblies": normalized_assemblies}

    @staticmethod
    def _choose_route(profile: NativeCompileProfile, analysis: dict[str, Any]) -> str:
        changed = [item for item in analysis["assemblies"] if not item["structureEqual"] or item["changedMethods"]]
        if not changed:
            raise CommandError("RESOURCE_BUILD_FAILED", "Compile completed but no managed code or structure differs from baseline.", stage="compile_compare", runtime_changed=False, recoverable=False)
        if (
            len(changed) == 1
            and changed[0]["structureEqual"]
            and changed[0]["changedMethods"]
            and changed[0]["moduleId"] == profile.module_id
        ):
            return "HOTFIX"
        return "MODULE_RELOAD"

    def _build_runtime_manifest(
        self,
        job_id: str,
        task: dict[str, Any],
        profile: NativeCompileProfile,
        binding: dict[str, Any],
        analysis: dict[str, Any],
        route: str,
        registered: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        state = binding["runtimeState"]
        generations = {
            "moduleGeneration": state["moduleGeneration"],
            "resourceRelease": state["resourceRelease"],
            "viewGeneration": state["viewGeneration"],
        }
        common = {
            "schema": MANIFEST_SCHEMA,
            "version": 1,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "launchId": binding["launchId"],
            "inputSnapshot": binding["inputSnapshot"],
            "route": route,
            "moduleId": profile.module_id,
            "dependencyClosure": list(profile.dependency_closure),
            "expectedRuntimeRevision": state["runtimeRevision"],
            "targetGenerations": generations,
            "affectedViews": [],
        }
        if route == "HOTFIX":
            changed = next(item for item in analysis["assemblies"] if not item["structureEqual"] or item["changedMethods"])
            file_record = analysis["filesByAssembly"][changed["name"]][".dll"]
            runtime_artifact = self._register_alias(
                job_id, task, registered[file_record["artifactId"]], "runtime_hotfix_assembly", "hotfix-dll"
            )
            types_by_name: dict[str, set[str]] = {}
            for method in changed["changedMethods"]:
                types_by_name.setdefault(method["typeName"], set()).add(method["signature"])
            if not types_by_name or any(not values for values in types_by_name.values()):
                raise CommandError("RESOURCE_BUILD_FAILED", "Hotfix analysis lacks exact changed method signatures.", stage="compile_compare", runtime_changed=False, recoverable=False)
            common.update(
                {
                    "expectedRuntimeRevisionAfter": runtime_revision_after(state["runtimeRevision"], task["taskId"], "hotfix"),
                    "targetGenerationsAfter": dict(generations),
                    "requiredImpact": {
                        "hotfix": True, "rebuildViews": [], "reloadModules": [], "restartPlayer": False, "buildBaseline": False,
                    },
                    "affectedModules": [profile.module_id],
                    "assemblyArtifactId": runtime_artifact["artifactId"],
                    "assemblyName": changed["name"],
                    "expectedAssemblyGeneration": state["moduleGeneration"],
                    "types": [
                        {"typeName": type_name, "signatures": sorted(signatures)}
                        for type_name, signatures in sorted(types_by_name.items())
                    ],
                }
            )
            return common, [runtime_artifact["artifactId"]]

        candidate_file_ids: list[str] = []
        payloads = []
        for assembly in profile.assemblies:
            dll_record = analysis["filesByAssembly"][assembly.name][".dll"]
            dll = self._register_alias(job_id, task, registered[dll_record["artifactId"]], "runtime_reload_dll", f"reload-dll:{assembly.name}")
            candidate_file_ids.append(dll["artifactId"])
            pdb_record = analysis["filesByAssembly"][assembly.name].get(".pdb")
            pdb_id = None
            if pdb_record is not None:
                pdb = self._register_alias(job_id, task, registered[pdb_record["artifactId"]], "runtime_reload_pdb", f"reload-pdb:{assembly.name}")
                pdb_id = pdb["artifactId"]
                candidate_file_ids.append(pdb_id)
            payloads.append(
                {
                    "name": assembly.name,
                    "expectedGeneration": state["moduleGeneration"],
                    "generationAfter": state["moduleGeneration"] + 1,
                    "dllArtifactId": dll["artifactId"],
                    "pdbArtifactId": pdb_id,
                }
            )
        next_generation = state["moduleGeneration"] + 1
        common.update(
            {
                "expectedRuntimeRevisionAfter": None,
                "targetGenerationsAfter": {
                    "moduleGeneration": next_generation,
                    "resourceRelease": state["resourceRelease"],
                    "viewGeneration": state["viewGeneration"] + 1,
                },
                "requiredImpact": {
                    "hotfix": False,
                    "rebuildViews": [],
                    "reloadModules": list(profile.dependency_closure),
                    "restartPlayer": False,
                    "buildBaseline": False,
                },
                "affectedModules": list(profile.dependency_closure),
                "resourceRelease": state["resourceRelease"],
                "disposedGeneration": state["moduleGeneration"],
                "nextGeneration": next_generation,
                "entryAssemblyName": profile.entry_assembly_name,
                "authorizedModuleClosure": list(profile.dependency_closure),
                "assemblies": [
                    {"name": item.name, "dependencies": list(item.dependencies)} for item in profile.assemblies
                ],
                "payloads": payloads,
            }
        )
        return common, candidate_file_ids

    def _register_alias(
        self,
        job_id: str,
        task: dict[str, Any],
        source: dict[str, Any],
        kind: str,
        label: str,
    ) -> dict[str, Any]:
        record = self._artifacts.ledger.get_artifact(source["artifactId"])
        return self._artifacts.register(
            record["absolutePath"],
            kind=kind,
            expected_sha256=source["sha256"],
            expected_size=source["size"],
            artifact_id=_id_for(job_id, label),
            task_id=task["taskId"],
            job_id=job_id,
        )

    def _persist_generated_manifest(self, job_id: str, raw: bytes) -> Path:
        root = self._artifacts.allowed_roots[0]
        directory = root / "native-preparation" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "runtime-update-manifest.json"
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise CommandError("INPUT_CHANGED", "Previously generated runtime manifest cannot be read.", stage="prepare", runtime_changed=False, recoverable=False) from exc
            if existing != raw:
                raise CommandError("INPUT_CHANGED", "Prepare job already produced different runtime manifest bytes.", stage="prepare", runtime_changed=False, recoverable=False)
            return path
        try:
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != raw:
                raise CommandError("INPUT_CHANGED", "Concurrent runtime manifest creation produced different bytes.", stage="prepare", runtime_changed=False, recoverable=False)
        return path

    def _verify_receipt_artifact(
        self,
        artifact_id: str,
        expected_sha256: str,
        profile: NativeCompileProfile,
        *,
        job_id: str,
        task_id: str,
        input_snapshot: str,
        expected_output_files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        record = self._ledger.get_artifact(artifact_id)
        if record.get("taskId") != task_id or record.get("jobId") != job_id:
            raise CommandError("AUTH_REQUIRED", "Compile receipt ownership differs from its task/job.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        if record.get("kind") != "native_compile_input_receipt" or record.get("sha256") != expected_sha256:
            raise CommandError("INPUT_CHANGED", "Compile receipt artifact identity/hash differs from the analysis binding.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        metadata, stream = self._artifacts.open_verified(artifact_id)
        try:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
        finally:
            stream.close()
        if len(raw) > MAX_RECEIPT_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Compile input receipt exceeds its size limit.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        if metadata.get("sha256") != expected_sha256:
            raise CommandError("INPUT_CHANGED", "Compile receipt bytes changed after registration.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        try:
            receipt = _parse_object(raw, "Native compile input receipt")
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Compile input receipt is not unique-field UTF-8 JSON.", stage="compile_input_receipt", runtime_changed=False, recoverable=False) from exc
        if receipt.get("inputSnapshot") != input_snapshot:
            raise CommandError("INPUT_CHANGED", "Compile input receipt source snapshot differs from the prepare binding.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        return validate_native_compile_input_receipt(
            receipt,
            profile,
            job_id=job_id,
            receipt_path=record["absolutePath"],
            expected_output_files=expected_output_files,
        )

    def verify_preparation_receipt(
        self,
        task: dict[str, Any],
        evidence: dict[str, Any],
        *,
        expected_input_snapshot: str,
    ) -> None:
        """Revalidate the exact sealed compiler receipt immediately before plan creation/apply."""
        profile = self.profile_for_task(task)
        profile_digest = native_compile_profile_digest(profile)
        required = {
            "providerId", "profileId", "profileDigest", "editorJobId", "editorRequestDigest", "analysisArtifactId",
            "compileInputReceiptArtifactId", "compileInputReceiptSha256", "compileManifestArtifactId",
            "runtimeManifestArtifactId", "editorCandidateArtifactIds", "assemblyDiffs", "compilerInputCoverage",
            "compilerInputLimitations",
        }
        try:
            evidence_limitations = validate_compiler_input_coverage(evidence)
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Preparation evidence omits required compiler input limitations.", stage="compile_input_receipt", runtime_changed=False, recoverable=False) from exc
        if (
            not isinstance(evidence, dict)
            or set(evidence) != required
            or evidence.get("providerId") != self.provider_id
            or evidence.get("profileId") != profile.profile_id
            or evidence.get("profileDigest") != profile_digest
            or not isinstance(evidence.get("editorJobId"), str)
            or not ID_RE.fullmatch(evidence["editorJobId"])
            or not isinstance(evidence.get("analysisArtifactId"), str)
            or not ID_RE.fullmatch(evidence["analysisArtifactId"])
            or not isinstance(evidence.get("compileInputReceiptArtifactId"), str)
            or not ID_RE.fullmatch(evidence["compileInputReceiptArtifactId"])
            or not isinstance(evidence.get("compileInputReceiptSha256"), str)
            or not SHA256_RE.fullmatch(evidence["compileInputReceiptSha256"])
            or not isinstance(evidence.get("compileManifestArtifactId"), str)
            or not ID_RE.fullmatch(evidence["compileManifestArtifactId"])
            or not isinstance(evidence.get("editorCandidateArtifactIds"), list)
            or evidence["compileInputReceiptArtifactId"] not in evidence["editorCandidateArtifactIds"]
            or evidence["analysisArtifactId"] not in evidence["editorCandidateArtifactIds"]
            or evidence["compileManifestArtifactId"] not in evidence["editorCandidateArtifactIds"]
            or not isinstance(expected_input_snapshot, str)
        ):
            raise CommandError("INPUT_CHANGED", "Compile receipt evidence differs from the current profile/plan binding.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        job_id = evidence["editorJobId"]
        analysis_id = evidence["analysisArtifactId"]
        analysis_record = self._ledger.get_artifact(analysis_id)
        if (
            analysis_record.get("taskId") != task["taskId"]
            or analysis_record.get("jobId") != job_id
            or analysis_record.get("kind") != "native_compile_analysis"
        ):
            raise CommandError("AUTH_REQUIRED", "Sealed compile analysis ownership differs from its task/job.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        try:
            analysis = _parse_object(self._read_artifact(analysis_id, MAX_MANIFEST_BYTES), "Sealed native compile analysis")
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Sealed native compile analysis is malformed during receipt recheck.", stage="compile_input_receipt", runtime_changed=False, recoverable=False) from exc
        if (
            analysis.get("schema") != ANALYSIS_SCHEMA
            or analysis.get("version") != 3
            or analysis.get("profileId") != profile.profile_id
            or analysis.get("profileDigest") != profile_digest
            or analysis.get("taskId") != task["taskId"]
            or analysis.get("sessionId") != task["sessionId"]
            or analysis.get("jobId") != job_id
            or analysis.get("inputSnapshot") != expected_input_snapshot
            or not isinstance(analysis.get("analysisArtifactId"), str)
            or _id_for(job_id, "editor", analysis["analysisArtifactId"]) != evidence["analysisArtifactId"]
            or not isinstance(analysis.get("compileInputReceiptArtifactId"), str)
            or _id_for(job_id, "editor", analysis["compileInputReceiptArtifactId"]) != evidence["compileInputReceiptArtifactId"]
            or analysis.get("compileInputReceiptSha256") != evidence["compileInputReceiptSha256"]
            or not isinstance(analysis.get("compileManifestArtifactId"), str)
            or _id_for(job_id, "editor", analysis["compileManifestArtifactId"]) != evidence["compileManifestArtifactId"]
            or not isinstance(analysis.get("files"), list)
        ):
            raise CommandError("INPUT_CHANGED", "Sealed compile analysis differs from the plan receipt binding.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
        expected_outputs = []
        for item in analysis["files"]:
            if not isinstance(item, dict) or set(item) != {"artifactId", "assemblyName", "extension", "sha256", "size"}:
                raise CommandError("CONTRACT_MISMATCH", "Sealed analysis output fields are malformed during receipt recheck.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)
            expected_outputs.append({
                "assemblyName": item["assemblyName"],
                "extension": item["extension"],
                "sha256": item["sha256"],
                "size": item["size"],
            })
        verified_receipt = self._verify_receipt_artifact(
            evidence["compileInputReceiptArtifactId"],
            evidence["compileInputReceiptSha256"],
            profile,
            job_id=job_id,
            task_id=task["taskId"],
            input_snapshot=expected_input_snapshot,
            expected_output_files=expected_outputs,
        )
        if evidence_limitations != verified_receipt["limitations"]:
            raise CommandError("INPUT_CHANGED", "Preparation compiler input limitations differ from the sealed receipt.", stage="compile_input_receipt", runtime_changed=False, recoverable=False)

    def _verify_saved_provider_result(
        self,
        job_id: str,
        task: dict[str, Any],
        profile: NativeCompileProfile,
        binding: dict[str, Any],
        saved: dict[str, Any],
    ) -> dict[str, Any]:
        self._verify_binding(task, profile, binding, job_id=job_id)
        evidence = saved.get("preparationEvidence")
        if (
            saved.get("profileDigest") != binding["profileDigest"]
            or not isinstance(evidence, dict)
            or evidence.get("profileDigest") != binding["profileDigest"]
            or saved.get("inputSnapshot") != binding["inputSnapshot"]
            or saved.get("expectedRuntimeRevision") != binding["runtimeState"].get("runtimeRevision")
        ):
            raise CommandError("INPUT_CHANGED", "Saved preparation result differs from its durable input/runtime binding.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        if self._runtime.observe_task_state(task) != binding["runtimeState"]:
            raise CommandError("STALE_TARGET", "Player state changed before recovered plan creation.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        for metadata in saved.get("artifacts", []):
            if not isinstance(metadata, dict) or not isinstance(metadata.get("artifactId"), str):
                raise CommandError("CONTRACT_MISMATCH", "Saved preparation artifact metadata is invalid.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
            record = self._ledger.get_artifact(metadata["artifactId"])
            if record.get("taskId") != task["taskId"] or record.get("jobId") != job_id:
                raise CommandError("AUTH_REQUIRED", "Recovered candidate artifact ownership differs from the task/job.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
            actual, stream = self._artifacts.open_verified(metadata["artifactId"])
            stream.close()
            if actual != metadata:
                raise CommandError("INPUT_CHANGED", "Recovered preparation artifact metadata changed.", stage="prepare_reconcile", runtime_changed=False, recoverable=False)
        self.verify_preparation_receipt(task, evidence, expected_input_snapshot=binding["inputSnapshot"])
        return saved

    def _read_artifact(self, artifact_id: str, maximum: int) -> bytes:
        _metadata, stream = self._artifacts.open_verified(artifact_id)
        try:
            raw = stream.read(maximum + 1)
        finally:
            stream.close()
        if len(raw) > maximum:
            raise CommandError("CONTRACT_MISMATCH", "Native compile analysis artifact exceeds its size limit.", stage="editor_compile", runtime_changed=False, recoverable=False)
        return raw

    def _open_task_artifact(self, task_id: str, artifact_id: str) -> tuple[dict[str, Any], Any]:
        record = self._ledger.get_artifact(artifact_id)
        if record.get("taskId") != task_id:
            raise CommandError("AUTH_REQUIRED", "Candidate artifact is not registered to this task.", stage="prepare", runtime_changed=False, recoverable=False)
        return self._artifacts.open_verified(artifact_id)

    def _save_job(self, job_id: str, stage: str, changes: dict[str, Any]) -> None:
        job = self._ledger.get_job(job_id)
        result = dict(job.get("result") or {})
        result.update(changes)
        self._ledger.update_job(job_id, state="running", stage=stage, runtime_changed=False, result=result)
