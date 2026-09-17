from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import CommandError
from .validation import ID_RE, SHA256_RE


COMPOSITE_ROUTE = "MODULE_AND_ASSET_RELOAD"
COMPOSITE_BINDING_SCHEMA = "relay.liveloop.composite-candidate-binding"
COMPOSITE_EVIDENCE_SCHEMA = "relay.liveloop.composite-preparation-evidence"
RESOURCE_MANIFEST_SCHEMA = "relay.liveloop.resource-release-candidate"
MAX_RESOURCE_MANIFEST_BYTES = 256 * 1024
MAX_RESOURCE_ARCHIVE_BYTES = 6 * 1024 * 1024
MAX_COMPOSITE_ARTIFACTS = 128


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def artifact_closure_sha256(items: list[dict[str, Any]]) -> str:
    return _sha256(sorted(items, key=lambda item: item["artifactId"]))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _parse_object(raw: bytes, label: str) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _context_requirements(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaId", "schemaVersion", "mediaType"}
        or not isinstance(value["schemaId"], str)
        or not value["schemaId"]
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] < 1
        or not isinstance(value["mediaType"], str)
        or not value["mediaType"]
    ):
        raise ValueError("contextRequirements fields are invalid")
    return dict(value)


def _artifact_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"artifactId", "kind", "sha256", "mediaType", "size"}:
        raise ValueError("artifact metadata fields are invalid")
    if (
        not isinstance(value["artifactId"], str)
        or not ID_RE.fullmatch(value["artifactId"])
        or not isinstance(value["kind"], str)
        or not ID_RE.fullmatch(value["kind"])
        or not isinstance(value["sha256"], str)
        or not SHA256_RE.fullmatch(value["sha256"])
        or not isinstance(value["mediaType"], str)
        or type(value["size"]) is not int
        or value["size"] < 0
    ):
        raise ValueError("artifact metadata values are invalid")
    return dict(value)


class CompositePreparationProvider:
    """Join independently prepared code and resource candidates into one immutable plan input."""

    provider_id = "composite-code-resource-preparation"
    is_composite_preparation_provider = True

    def __init__(self, code_provider: Any, resource_provider: Any, artifacts: Any, ledger: Any) -> None:
        if code_provider is None or not callable(getattr(code_provider, "prepare", None)):
            raise ValueError("code_provider must expose prepare(task, request)")
        if resource_provider is None or not callable(getattr(resource_provider, "prepare", None)):
            raise ValueError("resource_provider must expose prepare(task, request)")
        if artifacts is None or not callable(getattr(artifacts, "open_verified", None)):
            raise ValueError("artifacts must expose verified immutable reads")
        if ledger is None or not callable(getattr(ledger, "get_artifact", None)) or not callable(getattr(ledger, "get_job", None)):
            raise ValueError("ledger must expose task/job artifact ownership reads")
        for provider in (code_provider, resource_provider):
            if not callable(getattr(provider, "current_input_snapshot", None)) or not callable(
                getattr(provider, "current_profile_digest", None)
            ):
                raise ValueError("both candidate providers must expose current input snapshots and profile digests")
        self.code_provider = code_provider
        self.resource_provider = resource_provider
        self._artifacts = artifacts
        self._ledger = ledger

    @property
    def is_verified(self) -> bool:
        return all(getattr(provider, "is_verified", False) is True for provider in (self.code_provider, self.resource_provider))

    @property
    def unverified_reason(self) -> str | None:
        for provider in (self.code_provider, self.resource_provider):
            reason = getattr(provider, "unverified_reason", None)
            if reason:
                return str(reason)
        return None if self.is_verified else "Both code and resource preparation providers require verified capability evidence."

    def current_input_snapshot(self, task: dict[str, Any]) -> str:
        value = {
            "schema": "relay.liveloop.composite-input-snapshot/1",
            "code": self.code_provider.current_input_snapshot(task),
            "resource": self.resource_provider.current_input_snapshot(task),
        }
        return "sha256:" + _sha256(value)

    def current_profile_digest(self, task: dict[str, Any]) -> str:
        value = {
            "schema": "relay.liveloop.composite-profile-digest/1",
            "code": self.code_provider.current_profile_digest(task),
            "resource": self.resource_provider.current_profile_digest(task),
        }
        for key in ("code", "resource"):
            digest = value[key]
            if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA256_RE.fullmatch(digest[7:]):
                raise CommandError("CONTRACT_MISMATCH", f"{key} provider returned an invalid profile digest.", stage="prepare", runtime_changed=False, recoverable=False)
        return "sha256:" + _sha256(value)

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        code_snapshot = self.code_provider.current_input_snapshot(task)
        resource_snapshot = self.resource_provider.current_input_snapshot(task)
        composite_snapshot = self.current_input_snapshot(task)
        composite_profile = self.current_profile_digest(task)
        if request.get("inputSnapshot") != composite_snapshot:
            raise CommandError("INPUT_CHANGED", "Composite code/resource inputs changed before preparation.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        if request.get("profileDigest") != composite_profile:
            raise CommandError("INPUT_CHANGED", "Composite profile differs from the coordinator binding.", stage="prepare", runtime_changed=False, recoverable=False)

        code_profile_digest = self.code_provider.current_profile_digest(task)
        code_profile = self._profile(self.code_provider, task)
        code_request = dict(request)
        code_request.update(
            {
                "inputSnapshot": code_snapshot,
                "requestedInputSnapshot": code_snapshot,
                "profileDigest": code_profile_digest,
                "profileId": getattr(code_profile, "profile_id", None),
                "compositeInputSnapshot": composite_snapshot,
            }
        )
        code_prepared = self.code_provider.prepare(task, code_request)

        resource_profile_digest = self.resource_provider.current_profile_digest(task)
        resource_profile = self._profile(self.resource_provider, task)
        resource_request = dict(request)
        resource_request.update(
            {
                "component": "resource",
                "inputSnapshot": resource_snapshot,
                "requestedInputSnapshot": resource_snapshot,
                "profileDigest": resource_profile_digest,
                "profileId": getattr(resource_profile, "profile_id", None),
                "compositeInputSnapshot": composite_snapshot,
            }
        )
        # A build failure aborts preparation. The coordinator cannot create a plan until both return.
        resource_prepared = self.resource_provider.prepare(task, resource_request)
        return self._compose(task, request, code_prepared, resource_prepared)

    def reconcile_prepare(self, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
        code_method = getattr(self.code_provider, "reconcile_prepare", None)
        resource_method = getattr(self.resource_provider, "reconcile_prepare", None)
        if not callable(code_method) or not callable(resource_method):
            return None
        code_prepared = code_method(job, task)
        resource_prepared = resource_method(job, task)
        if code_prepared is None or resource_prepared is None:
            return None
        request = (job.get("result") or {}).get("prepareCoordinatorBinding")
        if not isinstance(request, dict):
            return None
        runtime_state = request.get("runtimeState")
        if not isinstance(runtime_state, dict):
            return None
        synthetic_request = {
            "jobId": job["jobId"],
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "inputSnapshot": self.current_input_snapshot(task),
            "profileDigest": self.current_profile_digest(task),
            "expectedRuntimeRevision": runtime_state.get("runtimeRevision"),
            "runtimeState": runtime_state,
        }
        return self._compose(task, synthetic_request, code_prepared, resource_prepared)

    def _compose(
        self,
        task: dict[str, Any],
        request: dict[str, Any],
        code_prepared: Any,
        resource_prepared: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime_state = request.get("runtimeState")
        job_id = request.get("jobId")
        if (
            not isinstance(runtime_state, dict)
            or set(runtime_state) != {"sessionId", "runtimeRevision", "moduleGeneration", "resourceRelease", "viewGeneration"}
            or not isinstance(job_id, str)
            or not ID_RE.fullmatch(job_id)
        ):
            raise CommandError("CONTRACT_MISMATCH", "Composite preparation lacks its durable job and authenticated runtime state.", stage="prepare", runtime_changed=False, recoverable=False)
        code_snapshot = self.code_provider.current_input_snapshot(task)
        resource_snapshot = self.resource_provider.current_input_snapshot(task)
        code_digest = self.code_provider.current_profile_digest(task)
        resource_digest = self.resource_provider.current_profile_digest(task)
        composite_snapshot = self.current_input_snapshot(task)
        composite_digest = self.current_profile_digest(task)
        if request.get("inputSnapshot") != composite_snapshot or request.get("profileDigest") != composite_digest:
            raise CommandError("INPUT_CHANGED", "Composite input/profile changed before plan binding.", stage="source_snapshot", runtime_changed=False, recoverable=False)
        code, code_evidence = self._validate_code_candidate(
            task, job_id, runtime_state, code_prepared, code_snapshot, code_digest
        )
        resource, resource_evidence = self._validate_resource_candidate(
            task, job_id, runtime_state, resource_prepared, resource_snapshot, resource_digest
        )
        if set(code["artifactIds"]) & set(resource["artifactIds"]):
            raise CommandError("CONTRACT_MISMATCH", "Code and resource artifact closures overlap.", stage="prepare", runtime_changed=False, recoverable=False)

        combined_artifacts = code["artifacts"] + resource["artifacts"]
        if len(combined_artifacts) > MAX_COMPOSITE_ARTIFACTS:
            raise CommandError("CONTRACT_MISMATCH", "Composite candidate exceeds the bounded artifact count.", stage="prepare", runtime_changed=False, recoverable=False)
        after = {
            "moduleGeneration": runtime_state["moduleGeneration"] + 1,
            "resourceRelease": resource["resourceReleaseAfter"],
            "viewGeneration": runtime_state["viewGeneration"] + 1,
        }
        required_impact = {
            "hotfix": False,
            "rebuildViews": list(resource["affectedViews"]),
            "reloadModules": list(code["affectedModules"]),
            "restartPlayer": False,
            "buildBaseline": False,
        }
        binding = {
            "schema": COMPOSITE_BINDING_SCHEMA,
            "version": 1,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "inputSnapshot": composite_snapshot,
            "profileDigest": composite_digest,
            "runtimeState": dict(runtime_state),
            "code": {
                "providerId": code["providerId"],
                "artifactJobId": code["artifactJobId"],
                "inputSnapshot": code_snapshot,
                "profileDigest": code_digest,
                "route": "MODULE_RELOAD",
                "manifestArtifactId": code["manifestArtifactId"],
                "manifestSha256": code["manifestSha256"],
                "artifactIds": code["artifactIds"],
                "artifacts": code["artifacts"],
                "artifactClosureSha256": artifact_closure_sha256(code["artifacts"]),
                "requiredArtifactKinds": code["requiredArtifactKinds"],
                "affectedModules": code["affectedModules"],
            },
            "resource": {
                "providerId": resource["providerId"],
                "artifactJobId": resource["artifactJobId"],
                "profileId": resource["profileId"],
                "inputSnapshot": resource_snapshot,
                "profileDigest": resource_digest,
                "manifestArtifactId": resource["manifestArtifactId"],
                "manifestSha256": resource["manifestSha256"],
                "archiveArtifactId": resource["archiveArtifactId"],
                "archiveSha256": resource["archiveSha256"],
                "artifactIds": resource["artifactIds"],
                "artifacts": resource["artifacts"],
                "artifactClosureSha256": artifact_closure_sha256(resource["artifacts"]),
                "requiredArtifactKinds": resource["requiredArtifactKinds"],
                "resourceReleaseBefore": resource["resourceReleaseBefore"],
                "resourceReleaseAfter": resource["resourceReleaseAfter"],
                "contextRequirements": resource["contextRequirements"],
                "affectedViews": resource["affectedViews"],
                "editorEvidence": resource_evidence,
            },
        }
        evidence = {
            "schema": COMPOSITE_EVIDENCE_SCHEMA,
            "version": 1,
            "providerId": self.provider_id,
            "inputSnapshot": composite_snapshot,
            "profileDigest": composite_digest,
            "code": code_evidence,
            "resource": resource_evidence,
        }
        return {
            "inputSnapshot": composite_snapshot,
            "profileDigest": composite_digest,
            "expectedRuntimeRevision": runtime_state["runtimeRevision"],
            "expectedRuntimeRevisionAfter": None,
            "route": COMPOSITE_ROUTE,
            "requiredImpact": required_impact,
            "artifacts": combined_artifacts,
            "requiredArtifactKinds": sorted(set(code["requiredArtifactKinds"] + resource["requiredArtifactKinds"])),
            "affectedModules": code["affectedModules"],
            "affectedViews": resource["affectedViews"],
            "targetGenerations": {
                key: runtime_state[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")
            },
            "targetGenerationsAfter": after,
            "approvalRequired": code["approvalRequired"] or resource["approvalRequired"],
            "prepareComplete": True,
            "compositeBinding": binding,
            "preparationEvidence": evidence,
        }

    def _validate_code_candidate(
        self, task: dict[str, Any], job_id: str, state: dict[str, Any], value: Any, snapshot: str, digest: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        required = {
            "inputSnapshot", "profileDigest", "expectedRuntimeRevision", "route", "requiredImpact", "artifacts",
            "requiredArtifactKinds", "affectedModules", "affectedViews", "targetGenerations", "targetGenerationsAfter",
            "expectedRuntimeRevisionAfter",
            "approvalRequired", "prepareComplete", "preparationEvidence",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "Code candidate fields do not match the composite preparation contract.", stage="prepare", runtime_changed=False, recoverable=False)
        if (
            value["inputSnapshot"] != snapshot
            or value["profileDigest"] != digest
            or value["expectedRuntimeRevision"] != state["runtimeRevision"]
            or value["route"] != "MODULE_RELOAD"
            or value["prepareComplete"] is not True
            or value["expectedRuntimeRevisionAfter"] is not None
            or value["targetGenerations"] != {key: state[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")}
            or value["targetGenerationsAfter"] != {
                "moduleGeneration": state["moduleGeneration"] + 1,
                "resourceRelease": state["resourceRelease"],
                "viewGeneration": state["viewGeneration"] + 1,
            }
        ):
            raise CommandError("INPUT_CHANGED", "Code candidate does not match the observed composite target.", stage="prepare", runtime_changed=False, recoverable=False)
        evidence = value["preparationEvidence"]
        if not isinstance(evidence, dict) or evidence.get("providerId") != getattr(self.code_provider, "provider_id", None):
            raise CommandError("CONTRACT_MISMATCH", "Code candidate lacks its provider-bound preparation evidence.", stage="prepare", runtime_changed=False, recoverable=False)
        artifacts = self._verify_artifact_list(value["artifacts"], task["taskId"], job_id)
        ids = [item["artifactId"] for item in artifacts]
        if not ids or len(set(ids)) != len(ids):
            raise CommandError("CONTRACT_MISMATCH", "Code candidate artifact closure is empty or duplicated.", stage="prepare", runtime_changed=False, recoverable=False)
        kinds = value["requiredArtifactKinds"]
        if not isinstance(kinds, list) or len(set(kinds)) != len(kinds) or any(not isinstance(item, str) for item in kinds):
            raise CommandError("CONTRACT_MISMATCH", "Code candidate required artifact kinds are invalid.", stage="prepare", runtime_changed=False, recoverable=False)
        if not set(kinds).issubset({item["kind"] for item in artifacts}):
            raise CommandError("RESOURCE_BUILD_FAILED", "Code artifact closure omits a required payload kind.", stage="prepare", runtime_changed=False)
        manifest_id = evidence.get("runtimeManifestArtifactId")
        manifest_records = [item for item in artifacts if item["artifactId"] == manifest_id and item["kind"] == "runtime_update_manifest"]
        if len(manifest_records) != 1:
            raise CommandError("CONTRACT_MISMATCH", "Code candidate lacks its unique runtime update manifest.", stage="prepare", runtime_changed=False, recoverable=False)
        manifest_meta, manifest_raw = self._read_artifact(manifest_id, "runtime_update_manifest", task["taskId"], job_id, 1024 * 1024)
        try:
            manifest = _parse_object(manifest_raw, "Native runtime manifest")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Code runtime manifest is malformed.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        if (
            manifest.get("route") != "MODULE_RELOAD"
            or manifest.get("taskId") != task["taskId"]
            or manifest.get("sessionId") != task["sessionId"]
            or manifest.get("inputSnapshot") != snapshot
            or manifest.get("expectedRuntimeRevision") != state["runtimeRevision"]
        ):
            raise CommandError("INPUT_CHANGED", "Code runtime manifest differs from the active composite binding.", stage="prepare", runtime_changed=False, recoverable=False)
        affected = value["affectedModules"]
        if not isinstance(affected, list) or not affected or any(not isinstance(item, str) or not item for item in affected):
            raise CommandError("CONTRACT_MISMATCH", "Code candidate affectedModules is invalid.", stage="prepare", runtime_changed=False, recoverable=False)
        if type(value["approvalRequired"]) is not bool:
            raise CommandError("CONTRACT_MISMATCH", "Code candidate approvalRequired must be boolean.", stage="prepare", runtime_changed=False, recoverable=False)
        return {
            "providerId": self.code_provider.provider_id,
            "artifactJobId": job_id,
            "artifactIds": sorted(ids),
            "artifacts": artifacts,
            "requiredArtifactKinds": sorted(set(kinds)),
            "manifestArtifactId": manifest_id,
            "manifestSha256": manifest_meta["sha256"],
            "affectedModules": list(affected),
            "approvalRequired": value["approvalRequired"],
        }, dict(evidence)

    def _validate_resource_candidate(
        self, task: dict[str, Any], job_id: str, state: dict[str, Any], value: Any, snapshot: str, digest: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        required = {
            "inputSnapshot", "profileDigest", "expectedRuntimeRevision", "resourceReleaseBefore", "resourceReleaseAfter",
            "manifestArtifactId", "archiveArtifactId", "artifactIds", "artifacts", "requiredArtifactKinds",
            "contextRequirements", "affectedViews", "prepareComplete", "approvalRequired", "preparationEvidence",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise CommandError("CONTRACT_MISMATCH", "Resource candidate fields do not match the composite preparation contract.", stage="prepare", runtime_changed=False, recoverable=False)
        release_before, release_after = value["resourceReleaseBefore"], value["resourceReleaseAfter"]
        if (
            value["inputSnapshot"] != snapshot
            or value["profileDigest"] != digest
            or value["expectedRuntimeRevision"] != state["runtimeRevision"]
            or release_before != state["resourceRelease"]
            or not isinstance(release_after, str)
            or not release_after
            or release_after == release_before
            or value["prepareComplete"] is not True
            or type(value["approvalRequired"]) is not bool
        ):
            raise CommandError("INPUT_CHANGED", "Resource candidate does not match the observed release/input binding.", stage="prepare", runtime_changed=False, recoverable=False)
        metadata = self._verify_artifact_list(value["artifacts"], task["taskId"], job_id)
        ids = value["artifactIds"]
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or len(set(ids)) != 2
            or set(ids) != {value["manifestArtifactId"], value["archiveArtifactId"]}
            or {item["artifactId"] for item in metadata} != set(ids)
        ):
            raise CommandError("CONTRACT_MISMATCH", "Resource candidate closure must contain exactly its manifest and archive.", stage="prepare", runtime_changed=False, recoverable=False)
        manifest_meta, raw = self._read_artifact(value["manifestArtifactId"], "resource_release_manifest", task["taskId"], job_id, MAX_RESOURCE_MANIFEST_BYTES)
        archive_meta, archive = self._read_artifact(value["archiveArtifactId"], "resource_release_archive", task["taskId"], job_id, MAX_RESOURCE_ARCHIVE_BYTES)
        if not archive or len(archive) > MAX_RESOURCE_ARCHIVE_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Resource archive is empty or exceeds its bounded size.", stage="prepare", runtime_changed=False, recoverable=False)
        try:
            manifest = _parse_object(raw, "Resource release manifest")
            context = _context_requirements(value["contextRequirements"])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Resource manifest or context requirements are malformed.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        expected_manifest = {
            "schema": RESOURCE_MANIFEST_SCHEMA,
            "version": 1,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "inputSnapshot": snapshot,
            "profileDigest": digest,
            "runtimeRevisionBefore": state["runtimeRevision"],
            "resourceReleaseBefore": release_before,
            "resourceReleaseAfter": release_after,
            "archiveArtifactId": archive_meta["artifactId"],
            "archiveSha256": archive_meta["sha256"],
            "archiveSize": archive_meta["size"],
            "contextRequirements": context,
            "affectedViews": value["affectedViews"],
        }
        if manifest != expected_manifest:
            raise CommandError("INPUT_CHANGED", "Resource manifest differs from its authenticated preparation result or archive bytes.", stage="prepare", runtime_changed=False, recoverable=False)
        affected_views = value["affectedViews"]
        if not isinstance(affected_views, list) or len(set(affected_views)) != len(affected_views) or any(not isinstance(item, str) or not item for item in affected_views):
            raise CommandError("CONTRACT_MISMATCH", "Resource candidate affectedViews is invalid.", stage="prepare", runtime_changed=False, recoverable=False)
        evidence = value["preparationEvidence"]
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"providerId", "profileId", "editorJobId", "editorRequestDigest"}
            or evidence.get("providerId") != getattr(self.resource_provider, "provider_id", None)
            or not isinstance(evidence.get("profileId"), str)
            or not ID_RE.fullmatch(evidence["profileId"])
            or not isinstance(evidence.get("editorJobId"), str)
            or not ID_RE.fullmatch(evidence["editorJobId"])
            or not isinstance(evidence.get("editorRequestDigest"), str)
            or not SHA256_RE.fullmatch(evidence["editorRequestDigest"])
        ):
            raise CommandError("CONTRACT_MISMATCH", "Resource candidate lacks sealed asset-build evidence.", stage="prepare", runtime_changed=False, recoverable=False)
        profile = self._profile(self.resource_provider, task)
        if getattr(profile, "profile_id", evidence["profileId"]) != evidence["profileId"]:
            raise CommandError("INPUT_CHANGED", "Resource evidence profile differs from the current server profile.", stage="prepare", runtime_changed=False, recoverable=False)
        kinds = value["requiredArtifactKinds"]
        if not isinstance(kinds, list) or set(kinds) != {"resource_release_manifest", "resource_release_archive"}:
            raise CommandError("CONTRACT_MISMATCH", "Resource candidate required artifact kinds are invalid.", stage="prepare", runtime_changed=False, recoverable=False)
        return {
            "providerId": self.resource_provider.provider_id,
            "artifactJobId": job_id,
            "profileId": evidence["profileId"],
            "artifactIds": sorted(ids),
            "artifacts": metadata,
            "requiredArtifactKinds": sorted(set(kinds)),
            "manifestArtifactId": manifest_meta["artifactId"],
            "manifestSha256": manifest_meta["sha256"],
            "archiveArtifactId": archive_meta["artifactId"],
            "archiveSha256": archive_meta["sha256"],
            "resourceReleaseBefore": release_before,
            "resourceReleaseAfter": release_after,
            "contextRequirements": context,
            "affectedViews": list(affected_views),
            "approvalRequired": value["approvalRequired"],
        }, dict(evidence)

    def verify_prepared_result(self, task: dict[str, Any], value: dict[str, Any], runtime_state: dict[str, Any] | None, job_id: str | None) -> None:
        evidence = value.get("preparationEvidence")
        binding = value.get("compositeBinding")
        if not isinstance(evidence, dict) or not isinstance(binding, dict):
            raise CommandError("CONTRACT_MISMATCH", "Composite preparation result lacks its immutable candidate binding.", stage="prepare", runtime_changed=False, recoverable=False)
        self.verify_preparation_plan(
            task,
            evidence,
            binding,
            value.get("artifacts"),
            value.get("inputSnapshot"),
            expected_runtime_state=runtime_state,
            job_id=job_id,
        )
        expected_targets = {
            key: binding["runtimeState"][key]
            for key in ("moduleGeneration", "resourceRelease", "viewGeneration")
        }
        expected_after = {
            "moduleGeneration": expected_targets["moduleGeneration"] + 1,
            "resourceRelease": binding["resource"]["resourceReleaseAfter"],
            "viewGeneration": expected_targets["viewGeneration"] + 1,
        }
        expected_impact = {
            "hotfix": False,
            "rebuildViews": binding["resource"]["affectedViews"],
            "reloadModules": binding["code"]["affectedModules"],
            "restartPlayer": False,
            "buildBaseline": False,
        }
        if (
            value.get("route") != COMPOSITE_ROUTE
            or value.get("profileDigest") != self.current_profile_digest(task)
            or value.get("inputSnapshot") != self.current_input_snapshot(task)
            or value.get("targetGenerations") != expected_targets
            or value.get("targetGenerationsAfter") != expected_after
            or value.get("expectedRuntimeRevision") != binding["runtimeState"]["runtimeRevision"]
            or value.get("expectedRuntimeRevisionAfter") is not None
            or value.get("requiredImpact") != expected_impact
            or value.get("affectedModules") != expected_impact["reloadModules"]
            or value.get("affectedViews") != expected_impact["rebuildViews"]
        ):
            raise CommandError("INPUT_CHANGED", "Composite preparation result no longer matches its component binding.", stage="prepare", runtime_changed=False, recoverable=False)

    def verify_preparation_plan(
        self,
        task: dict[str, Any],
        evidence: Any,
        binding: Any,
        artifacts: Any,
        input_snapshot: Any,
        *,
        expected_runtime_state: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> None:
        try:
            self._validate_binding_shape(task, evidence, binding, input_snapshot, expected_runtime_state)
            if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_COMPOSITE_ARTIFACTS:
                raise ValueError("plan artifact closure is empty or oversized")
            code = binding["code"]
            resource = binding["resource"]
            combined = [_artifact_metadata(item) for item in artifacts]
            by_id = {item["artifactId"]: item for item in combined}
            if len(by_id) != len(combined):
                raise ValueError("plan artifact metadata contains duplicate identities")
            for component in (code, resource):
                component_artifacts = self._verify_artifact_list(
                    component["artifacts"], task["taskId"], component["artifactJobId"]
                )
                if artifact_closure_sha256(component_artifacts) != component["artifactClosureSha256"]:
                    raise ValueError("component artifact closure digest differs")
                if set(component["artifactIds"]) != {item["artifactId"] for item in component_artifacts}:
                    raise ValueError("component artifact ids differ from its closure")
                for metadata in component_artifacts:
                    if by_id.get(metadata["artifactId"]) != metadata:
                        raise ValueError("component closure differs from the immutable plan artifact set")
            if set(by_id) != set(code["artifactIds"]) | set(resource["artifactIds"]):
                raise ValueError("plan artifact set is not exactly the code/resource closure")
            if evidence["code"].get("runtimeManifestArtifactId") != code["manifestArtifactId"]:
                raise ValueError("code evidence does not bind the code manifest")
            if (
                evidence["code"].get("providerId") != code["providerId"]
                or evidence["code"].get("profileDigest") != code["profileDigest"]
                or evidence["code"].get("editorJobId") != code["artifactJobId"]
            ):
                raise ValueError("code evidence differs from the code profile or owner job")
            if evidence["resource"] != resource["editorEvidence"]:
                raise ValueError("resource evidence differs from the resource candidate binding")
            self._verify_code_manifest(task, binding, job_id)
            self._verify_resource_manifest(task, binding, job_id)
            expected_code_snapshot = code["inputSnapshot"]
            verifier = getattr(self.code_provider, "verify_preparation_receipt", None)
            if callable(verifier):
                verifier(task, evidence["code"], expected_input_snapshot=expected_code_snapshot)
            resource_verifier = getattr(self.resource_provider, "verify_preparation_receipt", None)
            if callable(resource_verifier):
                resource_verifier(task, evidence["resource"], expected_input_snapshot=resource["inputSnapshot"])
        except CommandError:
            raise
        except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
            raise CommandError("INPUT_CHANGED", "Composite code/resource candidate binding failed revalidation.", stage="prepare", runtime_changed=False, recoverable=False) from exc

    def _validate_binding_shape(
        self, task: dict[str, Any], evidence: Any, binding: Any, input_snapshot: Any, runtime_state: dict[str, Any] | None
    ) -> None:
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"schema", "version", "providerId", "inputSnapshot", "profileDigest", "code", "resource"}
            or evidence.get("schema") != COMPOSITE_EVIDENCE_SCHEMA
            or type(evidence.get("version")) is not int
            or evidence["version"] != 1
            or evidence.get("providerId") != self.provider_id
            or not isinstance(binding, dict)
            or set(binding) != {"schema", "version", "taskId", "sessionId", "inputSnapshot", "profileDigest", "runtimeState", "code", "resource"}
            or binding.get("schema") != COMPOSITE_BINDING_SCHEMA
            or type(binding.get("version")) is not int
            or binding["version"] != 1
            or binding.get("taskId") != task["taskId"]
            or binding.get("sessionId") != task["sessionId"]
            or not isinstance(binding.get("runtimeState"), dict)
            or binding.get("inputSnapshot") != input_snapshot
            or binding.get("inputSnapshot") != self.current_input_snapshot(task)
            or binding.get("profileDigest") != self.current_profile_digest(task)
            or evidence.get("inputSnapshot") != binding.get("inputSnapshot")
            or evidence.get("profileDigest") != binding.get("profileDigest")
            or evidence.get("code") is None
            or evidence.get("resource") is None
        ):
            raise ValueError("composite evidence or plan binding fields differ")
        if runtime_state is not None and binding["runtimeState"] != runtime_state:
            raise ValueError("composite runtime observation differs")
        code, resource = binding.get("code"), binding.get("resource")
        code_fields = {
            "providerId", "inputSnapshot", "profileDigest", "route", "manifestArtifactId", "manifestSha256",
            "artifactJobId", "artifactIds", "artifacts", "artifactClosureSha256", "requiredArtifactKinds", "affectedModules",
        }
        resource_fields = {
            "providerId", "artifactJobId", "profileId", "inputSnapshot", "profileDigest", "manifestArtifactId", "manifestSha256",
            "archiveArtifactId", "archiveSha256", "artifactIds", "artifacts", "artifactClosureSha256",
            "requiredArtifactKinds", "resourceReleaseBefore", "resourceReleaseAfter", "contextRequirements",
            "affectedViews", "editorEvidence",
        }
        if not isinstance(code, dict) or set(code) != code_fields or code.get("route") != "MODULE_RELOAD":
            raise ValueError("code candidate binding fields differ")
        if not isinstance(resource, dict) or set(resource) != resource_fields:
            raise ValueError("resource candidate binding fields differ")
        if any(
            not isinstance(component.get("artifactJobId"), str) or not ID_RE.fullmatch(component["artifactJobId"])
            for component in (code, resource)
        ):
            raise ValueError("candidate artifact owner job identity is invalid")
        state = binding["runtimeState"]
        if set(state) != {"sessionId", "runtimeRevision", "moduleGeneration", "resourceRelease", "viewGeneration"}:
            raise ValueError("composite runtime observation fields differ")
        if resource["resourceReleaseBefore"] != state["resourceRelease"] or resource["resourceReleaseAfter"] == state["resourceRelease"]:
            raise ValueError("resource release transition is invalid")
        if code["inputSnapshot"] != self.code_provider.current_input_snapshot(task) or code["profileDigest"] != self.code_provider.current_profile_digest(task):
            raise ValueError("current code inputs differ")
        if resource["inputSnapshot"] != self.resource_provider.current_input_snapshot(task) or resource["profileDigest"] != self.resource_provider.current_profile_digest(task):
            raise ValueError("current resource inputs differ")

    def _verify_code_manifest(self, task: dict[str, Any], binding: dict[str, Any], job_id: str | None) -> None:
        code = binding["code"]
        metadata, raw = self._read_artifact(code["manifestArtifactId"], "runtime_update_manifest", task["taskId"], code["artifactJobId"], 1024 * 1024)
        manifest = _parse_object(raw, "Native runtime manifest")
        state = binding["runtimeState"]
        if (
            metadata["sha256"] != code["manifestSha256"]
            or manifest.get("route") != "MODULE_RELOAD"
            or manifest.get("taskId") != task["taskId"]
            or manifest.get("sessionId") != task["sessionId"]
            or manifest.get("inputSnapshot") != code["inputSnapshot"]
            or manifest.get("expectedRuntimeRevision") != state["runtimeRevision"]
            or manifest.get("targetGenerations") != {
                key: state[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")
            }
        ):
            raise ValueError("code manifest differs from the composite runtime binding")

    def _verify_resource_manifest(self, task: dict[str, Any], binding: dict[str, Any], job_id: str | None) -> None:
        resource = binding["resource"]
        manifest_meta, raw = self._read_artifact(resource["manifestArtifactId"], "resource_release_manifest", task["taskId"], resource["artifactJobId"], MAX_RESOURCE_MANIFEST_BYTES)
        archive_meta, archive = self._read_artifact(resource["archiveArtifactId"], "resource_release_archive", task["taskId"], resource["artifactJobId"], MAX_RESOURCE_ARCHIVE_BYTES)
        manifest = _parse_object(raw, "Resource release manifest")
        expected = {
            "schema": RESOURCE_MANIFEST_SCHEMA,
            "version": 1,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "inputSnapshot": resource["inputSnapshot"],
            "profileDigest": resource["profileDigest"],
            "runtimeRevisionBefore": binding["runtimeState"]["runtimeRevision"],
            "resourceReleaseBefore": resource["resourceReleaseBefore"],
            "resourceReleaseAfter": resource["resourceReleaseAfter"],
            "archiveArtifactId": archive_meta["artifactId"],
            "archiveSha256": archive_meta["sha256"],
            "archiveSize": archive_meta["size"],
            "contextRequirements": resource["contextRequirements"],
            "affectedViews": resource["affectedViews"],
        }
        if (
            manifest_meta["sha256"] != resource["manifestSha256"]
            or archive_meta["sha256"] != resource["archiveSha256"]
            or len(archive) != archive_meta["size"]
            or manifest != expected
        ):
            raise ValueError("resource manifest/archive differs from the composite runtime binding")

    def _verify_artifact_list(self, values: Any, task_id: str, job_id: str | None) -> list[dict[str, Any]]:
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_COMPOSITE_ARTIFACTS:
            raise ValueError("artifact closure is empty or oversized")
        result = []
        for raw in values:
            item = _artifact_metadata(raw)
            record = self._ledger.get_artifact(item["artifactId"])
            if record.get("taskId") != task_id or (job_id is not None and record.get("jobId") != job_id):
                raise CommandError("AUTH_REQUIRED", "Composite artifact ownership differs from its task/job.", stage="prepare", runtime_changed=False, recoverable=False)
            metadata, stream = self._artifacts.open_verified(item["artifactId"])
            stream.close()
            if metadata != item:
                raise CommandError("INPUT_CHANGED", "Composite artifact metadata differs from its immutable registration.", stage="prepare", runtime_changed=False, recoverable=False)
            result.append(item)
        return result

    def _read_artifact(self, artifact_id: str, kind: str, task_id: str, job_id: str | None, maximum: int) -> tuple[dict[str, Any], bytes]:
        record = self._ledger.get_artifact(artifact_id)
        if record.get("taskId") != task_id or (job_id is not None and record.get("jobId") != job_id) or record.get("kind") != kind:
            raise CommandError("AUTH_REQUIRED", "Composite candidate artifact is not registered to its task/job/kind.", stage="prepare", runtime_changed=False, recoverable=False)
        metadata, stream = self._artifacts.open_verified(artifact_id)
        try:
            raw = stream.read(maximum + 1)
        finally:
            stream.close()
        if len(raw) > maximum or len(raw) != metadata["size"]:
            raise CommandError("CONTRACT_MISMATCH", "Composite candidate artifact exceeds its bounded size.", stage="prepare", runtime_changed=False, recoverable=False)
        return metadata, raw

    @staticmethod
    def _profile(provider: Any, task: dict[str, Any]) -> Any | None:
        loader = getattr(provider, "profile_for_task", None)
        return loader(task) if callable(loader) else None
