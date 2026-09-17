from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from typing import Any
from uuid import uuid4

from .composite_update import (
    COMPOSITE_BINDING_SCHEMA,
    COMPOSITE_ROUTE,
    MAX_RESOURCE_ARCHIVE_BYTES,
    MAX_RESOURCE_MANIFEST_BYTES,
    RESOURCE_MANIFEST_SCHEMA,
    artifact_closure_sha256,
)
from .errors import CommandError, capability_unavailable
from .native_compile_receipt import validate_compiler_input_coverage
from .validation import ID_RE, SHA256_RE


MANIFEST_KIND = "runtime_update_manifest"
MANIFEST_SCHEMA = "relay.liveloop.runtime-update-manifest"
PREPARATION_SCHEMA = "relay.liveloop.native-update-preparation"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RUNTIME_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_FRAME_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_COMMAND_BYTES = 11 * 1024 * 1024
SUPPORTED_ROUTES = frozenset({"HOTFIX", "MODULE_RELOAD", COMPOSITE_ROUTE})
CONTEXT_FIELDS = {
    "schemaId", "schemaVersion", "mediaType", "sourceSessionId", "sourceModuleGeneration",
    "sourceViewGeneration", "dataRevision", "payloadBase64",
}


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _parse_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} must be unique-field UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _canonical_text(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(char in value for char in "\r\n\0"):
        raise ValueError(f"{label} must be non-empty canonical text.")
    value.encode("utf-8", errors="strict")
    return value


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} fields do not match the frozen contract.")
    return value


def _generation(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")
    return value


def _string_array(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or len(value) > 128
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a unique bounded string array.")
    return list(value)


def _impact(value: Any) -> dict[str, Any]:
    result = _object(
        value,
        {"hotfix", "rebuildViews", "reloadModules", "restartPlayer", "buildBaseline"},
        "requiredImpact",
    )
    if any(type(result[key]) is not bool for key in ("hotfix", "restartPlayer", "buildBaseline")):
        raise ValueError("requiredImpact boolean fields are invalid.")
    return {
        "hotfix": result["hotfix"],
        "rebuildViews": _string_array(result["rebuildViews"], "requiredImpact.rebuildViews"),
        "reloadModules": _string_array(result["reloadModules"], "requiredImpact.reloadModules"),
        "restartPlayer": result["restartPlayer"],
        "buildBaseline": result["buildBaseline"],
    }


def _generations(value: Any, label: str) -> dict[str, Any]:
    result = _object(value, {"moduleGeneration", "resourceRelease", "viewGeneration"}, label)
    return {
        "moduleGeneration": _generation(result["moduleGeneration"], f"{label}.moduleGeneration"),
        "resourceRelease": _canonical_text(result["resourceRelease"], f"{label}.resourceRelease"),
        "viewGeneration": _generation(result["viewGeneration"], f"{label}.viewGeneration"),
    }


def _without_digests(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_digests(item) for key, item in value.items() if key != "inputSha256"}
    if isinstance(value, list):
        return [_without_digests(item) for item in value]
    return value


def _validate_digest_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            key.encode("utf-8", errors="strict")
            _validate_digest_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_digest_values(item)
    elif isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif value is None or type(value) in {bool, int}:
        return
    else:
        raise ValueError("canonical payload values must be strings, integers, booleans, null, arrays, or objects")


def canonical_input_sha256(command: dict[str, Any]) -> str:
    """Hash compact ordinal-key UTF-8 JSON while excluding every inputSha256 field."""
    _validate_digest_values(command)
    canonical = json.dumps(
        _without_digests(command), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def runtime_revision_after(previous: str, task_id: str, stage: str) -> str:
    material = "\n".join(("relay.liveloop.runtime-revision-transition/1", previous, task_id, stage)).encode("utf-8")
    return "rr1:" + hashlib.sha256(material).hexdigest()


def _descriptor(reference: Any) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference or len(reference.encode("utf-8")) > 4000:
        raise CommandError("CAPABILITY_UNAVAILABLE", "Task reference must contain the Native update preparation descriptor.", stage="prepare", runtime_changed=False)
    try:
        value = json.loads(reference, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CommandError("CONTRACT_MISMATCH", "Task preparation reference is not unique-field JSON.", stage="prepare", runtime_changed=False, recoverable=False) from exc
    if not isinstance(value, dict) or set(value) != {"schema", "version", "inputSnapshot", "manifestArtifactId", "artifactIds"}:
        raise CommandError("CONTRACT_MISMATCH", "Task preparation descriptor fields do not match the frozen contract.", stage="prepare", runtime_changed=False, recoverable=False)
    if value["schema"] != PREPARATION_SCHEMA or type(value["version"]) is not int or value["version"] != 1:
        raise CommandError("CONTRACT_MISMATCH", "Task preparation descriptor schema/version is unsupported.", stage="prepare", runtime_changed=False, recoverable=False)
    try:
        input_snapshot = _canonical_text(value["inputSnapshot"], "inputSnapshot")
        manifest_id = value["manifestArtifactId"]
        if not isinstance(manifest_id, str) or not ID_RE.fullmatch(manifest_id):
            raise ValueError("manifestArtifactId is invalid")
        artifact_ids = value["artifactIds"]
        if (
            not isinstance(artifact_ids, list) or not artifact_ids or len(artifact_ids) > 128
            or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in artifact_ids)
            or len(set(artifact_ids)) != len(artifact_ids) or manifest_id not in artifact_ids
        ):
            raise ValueError("artifactIds must uniquely include manifestArtifactId")
    except (TypeError, ValueError) as exc:
        raise CommandError("CONTRACT_MISMATCH", "Task preparation descriptor values are invalid.", stage="prepare", runtime_changed=False, recoverable=False) from exc
    return {
        "schema": PREPARATION_SCHEMA,
        "version": 1,
        "inputSnapshot": input_snapshot,
        "manifestArtifactId": manifest_id,
        "artifactIds": list(artifact_ids),
    }


class PlayerRuntimeUpdateProvider:
    """Native update provider using only the frozen private operations and authenticated invoke()."""

    provider_id = "authenticated-player-runtime-update"

    def __init__(
        self,
        transport: Any,
        artifacts: Any,
        *,
        session_id: str,
        launch_id: str,
        runtime_revision: str,
        resource_release_id: str | None,
        profile_resolver: Any | None = None,
        ledger: Any | None = None,
    ) -> None:
        self._session_id = _canonical_text(session_id, "session_id")
        self._launch_id = _canonical_text(launch_id, "launch_id")
        self._runtime_revision = _canonical_text(runtime_revision, "runtime_revision")
        self._resource_release_id = None if resource_release_id is None else _canonical_text(resource_release_id, "resource_release_id")
        if profile_resolver is not None and not callable(getattr(profile_resolver, "for_task", None)):
            raise ValueError("profile_resolver must expose for_task(task).")
        if transport is None or not callable(getattr(transport, "invoke", None)):
            raise ValueError("transport must expose the existing authenticated invoke endpoint.")
        if not callable(getattr(transport, "update_expected_runtime_revision", None)) or not callable(getattr(transport, "connection_state", None)):
            raise ValueError("transport must expose revision advancement and authenticated connection identity.")
        if artifacts is None or not callable(getattr(artifacts, "open_verified", None)):
            raise ValueError("artifacts must expose verified immutable artifact reads.")
        self._transport = transport
        self._artifacts = artifacts
        self._profile_resolver = profile_resolver
        self._ledger = ledger
        self._module_generations: dict[str, int] = {}
        self._view_generations: dict[str, int] = {}
        self._last_states: dict[str, dict[str, Any]] = {}
        self._mutation_may_have_started = False
        self._session_uncertain = False
        self._lock = threading.RLock()

    @property
    def is_verified(self) -> bool:
        if self._session_uncertain or self._resource_release_id is None or not bool(getattr(self._transport, "authenticated", False)):
            return False
        try:
            self._assert_connection_identity()
        except CommandError:
            return False
        return True

    @property
    def unverified_reason(self) -> str | None:
        if self._resource_release_id is None:
            return "resourceReleaseId is required in the protected runtime session."
        if not bool(getattr(self._transport, "authenticated", False)):
            return "No authenticated Player connection is available."
        return None

    def probe_capability(self, _capability: str | None = None) -> bool:
        # Authentication is only transport evidence; route-specific preflight remains per-plan.
        return self.is_verified

    def observe_state(self, session_id: str) -> dict[str, Any]:
        # UpdateCoordinator calls observe_task_state when available so the private taskId is retained.
        if session_id != self._session_id:
            raise CommandError("WRONG_SESSION", "Runtime observation targets another configured Player session.", stage="runtime_state", runtime_changed=False, recoverable=False)
        raise CommandError("CAPABILITY_UNAVAILABLE", "Native runtime observation requires the caller task identity.", stage="runtime_state", runtime_changed=False, recoverable=False)

    def observe_task_state(self, task: dict[str, Any]) -> dict[str, Any]:
        if task.get("sessionId") != self._session_id:
            raise CommandError("WRONG_SESSION", "Task session does not match the authenticated Player session.", stage="runtime_state", runtime_changed=False, recoverable=False)
        self._require_configuration("runtime_state")
        with self._lock:
            profile = self._profile_resolver.for_task(task) if self._profile_resolver is not None else None
            if profile is None:
                descriptor = _descriptor(task.get("reference"))
                manifest, _metadata = self._read_task_manifest(task, descriptor)
                self._validate_manifest_identity(manifest, task, descriptor)
                module_id = self._module_id(manifest)
                target = _generations(manifest.get("targetGenerations"), "targetGenerations")
                initial_generation = target["moduleGeneration"]
                closure = _string_array(manifest.get("dependencyClosure"), "dependencyClosure", nonempty=True)
            else:
                module_id = profile.module_id
                initial_generation = profile.initial_module_generation
                closure = list(profile.dependency_closure)
            persisted = self._latest_module_version(module_id)
            persisted_release = None if persisted is None else persisted.get("metadata", {}).get("resourceRelease")
            expected_release = persisted_release if isinstance(persisted_release, str) and persisted_release else self._resource_release_id
            expected_generation = self._module_generations.get(
                module_id,
                persisted["generation"] if persisted is not None else initial_generation,
            )
            value, _reply = self._invoke(
                "module.observe",
                task["taskId"],
                {"moduleId": module_id, "expectedModuleGeneration": expected_generation, "dependencyClosure": closure},
                mutation=False,
            )
            result = value.get("result")
            if not isinstance(result, dict) or result.get("moduleId") != module_id:
                raise CommandError("WRONG_SESSION", "module.observe returned a missing or wrong module identity.", stage="runtime_state", runtime_changed=False, recoverable=False)
            module_generation = _generation(result.get("moduleGeneration"), "moduleGeneration")
            if module_generation != expected_generation:
                raise CommandError("STALE_TARGET", "module.observe generation differs from the plan-bound module.", stage="runtime_state", runtime_changed=False, recoverable=False)
            if result.get("stage") != "active":
                raise CommandError("STATE_UNKNOWN", "Target module is not in its stable active stage.", stage="runtime_state", runtime_changed=None, recoverable=False)
            self._validate_observed_release(result, expected_release=expected_release)

            context_value, _context_reply = self._invoke(
                "module.capture_context",
                task["taskId"],
                {"moduleId": module_id, "expectedModuleGeneration": module_generation, "dependencyClosure": closure},
                mutation=False,
            )
            context = context_value.get("result")
            self._validate_context(context, module_generation)
            view_generation = context["sourceViewGeneration"]
            observed_state = result.get("state")
            resource_release = (
                observed_state.get("resourceRelease")
                if isinstance(observed_state, dict) and isinstance(observed_state.get("resourceRelease"), str)
                else expected_release
            )
            if not isinstance(resource_release, str) or not resource_release:
                raise capability_unavailable("runtime_state", "An authenticated current resource release is unavailable.")
            persisted_view = None if persisted is None else persisted.get("metadata", {}).get("viewGeneration")
            expected_view = self._view_generations.get(module_id, persisted_view)
            if expected_view is not None and view_generation < expected_view:
                raise CommandError("STALE_TARGET", "Captured viewGeneration moved backwards.", stage="runtime_state", runtime_changed=False, recoverable=False)
            self._module_generations[module_id] = module_generation
            self._view_generations[module_id] = view_generation
            state = {
                "sessionId": self._session_id,
                "runtimeRevision": self._runtime_revision,
                "moduleGeneration": module_generation,
                "resourceRelease": resource_release,
                "viewGeneration": view_generation,
            }
            self._last_states[task["taskId"]] = dict(state)
            self._persist_module_observation(module_id, state)
            return state

    def prepare_manifest(
        self,
        task: dict[str, Any],
        descriptor: dict[str, Any],
        manifest: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            normalized, kinds, payload_ids = self._validate_manifest_fields(
                manifest,
                task_id=task["taskId"],
                session_id=task["sessionId"],
                input_snapshot=descriptor["inputSnapshot"],
                expected_revision=state["runtimeRevision"],
                target_generations={key: state[key] for key in ("moduleGeneration", "resourceRelease", "viewGeneration")},
            )
            if set(descriptor["artifactIds"]) != {descriptor["manifestArtifactId"], *payload_ids}:
                raise ValueError("descriptor artifactIds do not exactly cover the manifest closure")
            if not self._impact_subset(normalized["requiredImpact"], task["allowedImpact"]):
                normalized["approvalRequired"] = True
            else:
                normalized["approvalRequired"] = False
            normalized["prepareComplete"] = True
            return normalized, kinds
        except CommandError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Native update manifest does not match task/session/runtime inputs.", stage="prepare", runtime_changed=False, recoverable=False) from exc

    def apply(self, plan: dict[str, Any], job_id: str) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("route") not in SUPPORTED_ROUTES:
            raise capability_unavailable("runtime_apply", "The prepared route is not supported by this Player runtime provider.")
        if plan.get("sessionId") != self._session_id:
            raise CommandError("WRONG_SESSION", "Plan targets another Player session.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        if plan["route"] == COMPOSITE_ROUTE:
            if self._ledger is None:
                raise capability_unavailable("composite_runtime_apply", "Composite stage outcomes require the durable Host ledger.")
            job = self._ledger.get_job(job_id)
            journal = (job.get("result") or {}).get("runtimeStageJournal")
            if not isinstance(journal, list) or journal:
                raise CommandError(
                    "STATE_UNKNOWN",
                    "A composite runtime apply already has a durable stage record; automatic replay is forbidden.",
                    stage="runtime_apply",
                    runtime_changed=None,
                    recoverable=False,
                    details={"freshSessionRequired": True, "automaticReplayAllowed": False},
                )
        self._require_configuration("runtime_apply")
        with self._lock:
            self._mutation_may_have_started = False
            before = self._last_states.get(plan["taskId"])
            if before is None:
                raise CommandError("STATE_UNKNOWN", "No task-bound authenticated observation preceded apply.", stage="runtime_apply", runtime_changed=None, recoverable=False)
            if before["runtimeRevision"] != plan["expectedRuntimeRevision"]:
                raise CommandError("STALE_TARGET", "Plan runtime revision is stale.", stage="runtime_apply", runtime_changed=False)
            target = plan["details"].get("targetGenerations")
            if not isinstance(target, dict) or any(before[key] != target.get(key) for key in ("moduleGeneration", "resourceRelease", "viewGeneration")):
                raise CommandError("STALE_TARGET", "Plan target generations are stale.", stage="runtime_apply", runtime_changed=False)
            manifest = self._read_plan_manifest(plan)
            resource_candidate = self._read_resource_candidate(plan) if plan["route"] == COMPOSITE_ROUTE else None
            self._validate_plan_manifest(plan, manifest, resource_candidate=resource_candidate)
            if plan["route"] == "HOTFIX":
                applied = self._apply_hotfix(plan, manifest)
            else:
                applied = self._apply_module_reload(
                    plan,
                    manifest,
                    resource_candidate=resource_candidate,
                    job_id=job_id,
                )
            after = self._observe_plan_state(plan, manifest)
            expected_generations = (
                plan["details"]["targetGenerationsAfter"]
                if plan["route"] == COMPOSITE_ROUTE
                else manifest["targetGenerationsAfter"]
            )
            if any(after[key] != expected_generations[key] for key in expected_generations):
                return self._state_unknown("Player generations do not match the immutable manifest transition.", after["runtimeRevision"], applied)
            expected_revision = (
                plan["details"].get("expectedRuntimeRevisionAfter")
                if plan["route"] == COMPOSITE_ROUTE
                else manifest["expectedRuntimeRevisionAfter"]
            )
            if expected_revision is not None and after["runtimeRevision"] != expected_revision:
                return self._state_unknown("Player revision differs from the immutable hotfix transition.", after["runtimeRevision"], applied)
            if after["runtimeRevision"] == before["runtimeRevision"]:
                return self._state_unknown("Player reported completion without advancing its runtime revision.", after["runtimeRevision"], applied)
            return {
                "status": "completed",
                "runtimeChanged": True,
                "runtimeRevisionAfter": after["runtimeRevision"],
                "facts": {"runtimeMatched": True},
                "appliedSteps": applied,
                "methodStates": [],
                "error": None,
            }

    def reconcile(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            result_record = job.get("result") if isinstance(job.get("result"), dict) else {}
            journal = result_record.get("runtimeStageJournal")
            if plan.get("route") == COMPOSITE_ROUTE and isinstance(journal, list):
                acknowledged = [item for item in journal if isinstance(item, dict) and item.get("status") == "acknowledged"]
                uncertain = [
                    item for item in journal
                    if isinstance(item, dict) and item.get("status") in {"dispatching", "unknown"}
                ]
                failed = [
                    item for item in journal
                    if isinstance(item, dict) and item.get("status") in {"rejected", "failed_with_transition"}
                ]
                steps = [item.get("operation") for item in acknowledged if isinstance(item.get("operation"), str)]
                if uncertain:
                    uncertain_step = uncertain[-1].get("operation")
                    if isinstance(uncertain_step, str):
                        steps.append("uncertain:" + uncertain_step)
                    steps.append("fresh_session_required_no_replay")
                    return self._state_unknown(
                        "A composite runtime stage was dispatched without a durable authenticated terminal observation.",
                        None,
                        steps,
                    )
                if failed:
                    last_failure = failed[-1]
                    changed = bool(acknowledged) or last_failure.get("runtimeChanged") is True
                    after = last_failure.get("afterObservation")
                    revision = after.get("runtimeRevision") if isinstance(after, dict) else None
                    if revision is None and acknowledged:
                        after = acknowledged[-1].get("afterObservation")
                        revision = after.get("runtimeRevision") if isinstance(after, dict) else None
                    return {
                        "status": "failed",
                        "runtimeChanged": changed,
                        "runtimeRevisionAfter": revision,
                        "facts": {"runtimeMatched": False},
                        "appliedSteps": steps,
                        "methodStates": [],
                        "error": {
                            "code": "RUNTIME_APPLY_FAILED",
                            "stage": last_failure.get("operation", "runtime_apply"),
                            "message": "A composite runtime stage failed with a known terminal outcome; no rollback or replay was attempted.",
                            "recoverable": False,
                            "details": {
                                "automaticReplayAllowed": False,
                                "freshSessionRequired": True if changed else False,
                                "runtimeStageJournal": journal,
                            },
                        },
                    }
                if acknowledged:
                    steps.append("fresh_session_required_no_replay")
                    return self._state_unknown(
                        "Composite stages were acknowledged but a terminal plan result was not durably recorded.",
                        None,
                        steps,
                    )
            if self._mutation_may_have_started:
                # The frozen contract requires a fresh Player session after any possibly-dispatched mutation.
                steps = ["fresh_session_required_no_replay"]
                try:
                    manifest = self._read_plan_manifest(plan)
                    if bool(getattr(self._transport, "authenticated", False)):
                        self._invoke(
                            "module.reconcile",
                            plan["taskId"],
                            {"moduleId": manifest["moduleId"]},
                            mutation=False,
                        )
                        steps.append("module.reconcile_diagnostic_only")
                except Exception:
                    # Diagnostics may add evidence, but cannot clear the unknown-state latch.
                    pass
                return self._state_unknown("A Native mutation may have been dispatched; no replay or in-session recovery is allowed.", None, steps)
            previous = plan.get("expectedRuntimeRevision")
            known = self._last_states.get(plan.get("taskId"))
            if known is not None and known.get("runtimeRevision") == previous:
                return {
                    "status": "failed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": previous,
                    "facts": {},
                    "appliedSteps": ["reconciled_before_any_native_mutation"],
                    "methodStates": [],
                    "error": {
                        "code": "RUNTIME_APPLY_FAILED",
                        "stage": "runtime_reconcile",
                        "message": "No Native mutation was dispatched; the prepared revision remains unchanged.",
                        "recoverable": True,
                        "details": {"automaticReplayAllowed": False},
                    },
                }
            return self._state_unknown("Apply state cannot be established without a fresh session.", None, ["read_only_state_unavailable"])

    def _apply_hotfix(self, plan: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
        assembly = self._read_plan_artifact(plan, manifest["assemblyArtifactId"], "runtime_hotfix_assembly")
        expected_after = manifest["expectedRuntimeRevisionAfter"]
        if expected_after != runtime_revision_after(plan["expectedRuntimeRevision"], plan["taskId"], "hotfix"):
            raise CommandError("CONTRACT_MISMATCH", "Hotfix revision transition is not deterministic.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        assembly_hash = "sha256:" + hashlib.sha256(assembly).hexdigest()
        arguments = {
            "runtimeOperationId": self._operation_id(plan["taskId"], plan["expectedRuntimeRevision"], "hotfix"),
            "runtimeRevisionAfter": expected_after,
            "assemblyName": manifest["assemblyName"],
            "expectedAssemblyGeneration": manifest["expectedAssemblyGeneration"],
            "assemblyBase64": base64.b64encode(assembly).decode("ascii"),
            "assemblySha256": assembly_hash,
            "types": manifest["types"],
        }
        command = self._command("hotfix", plan["taskId"], arguments)
        command["arguments"]["inputSha256"] = canonical_input_sha256(command)
        value, reply = self._invoke("hotfix", plan["taskId"], command["arguments"], command=command, mutation=True)
        if reply.runtime_revision_after != expected_after or reply.runtime_changed is not True:
            raise self._contract_error("Hotfix terminal revision does not match its frozen transition.", "runtime_apply", None)
        result = value.get("result")
        if (
            not isinstance(result, dict)
            or result.get("assemblyName") != manifest["assemblyName"]
            or result.get("assemblyGeneration") != manifest["expectedAssemblyGeneration"]
            or result.get("appliedSha256") != assembly_hash
        ):
            raise self._contract_error("Hotfix result does not match the exact prepared bytes and identity.", "runtime_apply", True)
        return ["hotfix"]

    def _apply_module_reload(
        self,
        plan: dict[str, Any],
        manifest: dict[str, Any],
        *,
        resource_candidate: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> list[str]:
        module_id = manifest["moduleId"]
        closure = manifest["dependencyClosure"]
        generation = manifest["targetGenerations"]["moduleGeneration"]
        preflight, _ = self._invoke(
            "module.observe", plan["taskId"],
            {"moduleId": module_id, "expectedModuleGeneration": generation, "dependencyClosure": closure},
            mutation=False,
        )
        preflight_result = preflight.get("result")
        if preflight.get("nativeReady") is not True:
            raise capability_unavailable("native_module_reload", "Native-ready preflight refused the module before quiesce/dispose.")
        if (
            not isinstance(preflight_result, dict)
            or preflight_result.get("moduleId") != module_id
            or preflight_result.get("moduleGeneration") != generation
            or preflight_result.get("stage") != "active"
        ):
            raise CommandError("STALE_TARGET", "Module preflight identity/generation is not the prepared active target.", stage="runtime_apply", runtime_changed=False)
        self._validate_observed_release(
            preflight_result,
            expected_release=manifest["targetGenerations"]["resourceRelease"],
        )

        capture_value, _ = self._invoke(
            "module.capture_context", plan["taskId"],
            {"moduleId": module_id, "expectedModuleGeneration": generation, "dependencyClosure": closure},
            mutation=False,
        )
        context = capture_value.get("result")
        self._validate_context(context, generation)
        if context["sourceViewGeneration"] != manifest["targetGenerations"]["viewGeneration"]:
            raise CommandError("STALE_TARGET", "Captured context view generation differs from the prepared plan.", stage="runtime_apply", runtime_changed=False)
        if resource_candidate is not None:
            requirements = resource_candidate["manifest"]["contextRequirements"]
            if any(context.get(key) != requirements[key] for key in ("schemaId", "schemaVersion", "mediaType")):
                raise CommandError("STALE_TARGET", "Captured module context does not satisfy the prepared resource-release requirements.", stage="runtime_apply", runtime_changed=False, recoverable=False)

        candidate = self._reload_candidate(plan, manifest)
        load_arguments = {
            "moduleId": module_id,
            "expectedModuleGeneration": generation,
            "dependencyClosure": closure,
            "candidate": candidate,
        }
        load_command = self._command("module.load", plan["taskId"], load_arguments)
        load_command["arguments"]["candidate"]["inputSha256"] = canonical_input_sha256(load_command)
        self._encode_private_command(load_command)

        restore_arguments = {
            "moduleId": module_id,
            "expectedModuleGeneration": manifest["nextGeneration"],
            "dependencyClosure": closure,
            "context": context,
        }
        restore_command = self._command("module.restore", plan["taskId"], restore_arguments)
        self._encode_private_command(restore_command)

        resource_command = None
        if resource_candidate is not None:
            resource_manifest = resource_candidate["manifest"]
            resource_arguments = {
                "moduleId": module_id,
                "expectedModuleGeneration": manifest["nextGeneration"],
                "dependencyClosure": closure,
                "resourceReleaseBefore": resource_manifest["resourceReleaseBefore"],
                "resourceReleaseAfter": resource_manifest["resourceReleaseAfter"],
                "manifestBase64": base64.b64encode(resource_candidate["manifestBytes"]).decode("ascii"),
                "manifestSha256": "sha256:" + resource_candidate["manifestSha256"],
                "archiveBase64": base64.b64encode(resource_candidate["archiveBytes"]).decode("ascii"),
                "archiveSha256": "sha256:" + resource_candidate["archiveSha256"],
                "contextRequirements": resource_manifest["contextRequirements"],
            }
            resource_command = self._command("resource.activate", plan["taskId"], resource_arguments)
            resource_command["arguments"]["inputSha256"] = canonical_input_sha256(resource_command)
            self._encode_private_command(resource_command)

        applied = ["module.observe", "module.capture_context"]
        for operation in ("module.quiesce", "module.dispose"):
            arguments = {"moduleId": module_id, "expectedModuleGeneration": generation, "dependencyClosure": closure}
            if resource_candidate is None:
                _value, reply = self._invoke(operation, plan["taskId"], arguments, mutation=True)
            else:
                _value, reply = self._invoke_mutation_stage(
                    job_id,
                    operation,
                    plan["taskId"],
                    arguments,
                    before_release=manifest["targetGenerations"]["resourceRelease"],
                )
            self._mutation_may_have_started = True
            if reply.runtime_changed is not True or reply.runtime_revision_after is None:
                raise self._contract_error(f"{operation} did not publish an authenticated revision transition.", "runtime_apply", None)
            applied.append(operation)

        if resource_candidate is None:
            load_value, load_reply = self._invoke("module.load", plan["taskId"], load_command["arguments"], command=load_command, mutation=True)
        else:
            load_value, load_reply = self._invoke_mutation_stage(
                job_id,
                "module.load",
                plan["taskId"],
                load_command["arguments"],
                command=load_command,
                before_release=manifest["targetGenerations"]["resourceRelease"],
            )
        self._mutation_may_have_started = True
        if load_reply.runtime_changed is not True or load_reply.runtime_revision_after is None:
            raise self._contract_error("module.load did not publish an authenticated revision transition.", "runtime_apply", None)
        load_result = load_value.get("result")
        if not isinstance(load_result, dict) or load_result.get("moduleId") != module_id or load_result.get("moduleGeneration") != manifest["nextGeneration"]:
            raise self._contract_error("module.load result differs from the prepared module generation.", "runtime_apply", True)
        self._module_generations[module_id] = manifest["nextGeneration"]
        applied.append("module.load")

        if resource_command is not None:
            resource_manifest = resource_candidate["manifest"]
            resource_value, resource_reply = self._invoke_mutation_stage(
                job_id,
                "resource.activate",
                plan["taskId"],
                resource_command["arguments"],
                command=resource_command,
                before_release=resource_manifest["resourceReleaseBefore"],
            )
            self._mutation_may_have_started = True
            if resource_reply.runtime_changed is not True or resource_reply.runtime_revision_after is None:
                raise self._contract_error("resource.activate did not publish an authenticated revision transition.", "runtime_apply", None)
            resource_result = resource_value.get("result")
            if (
                not isinstance(resource_result, dict)
                or resource_result.get("moduleId") != module_id
                or resource_result.get("moduleGeneration") != manifest["nextGeneration"]
                or resource_result.get("resourceRelease") != resource_manifest["resourceReleaseAfter"]
                or resource_result.get("archiveSha256") != "sha256:" + resource_candidate["archiveSha256"]
                or resource_result.get("manifestSha256") != "sha256:" + resource_candidate["manifestSha256"]
            ):
                raise self._contract_error("resource.activate result differs from the exact prepared resource closure.", "runtime_apply", True)
            applied.append("resource.activate")

        if resource_candidate is None:
            restore_value, restore_reply = self._invoke(
                "module.restore", plan["taskId"], restore_command["arguments"], command=restore_command, mutation=True
            )
        else:
            restore_value, restore_reply = self._invoke_mutation_stage(
                job_id,
                "module.restore",
                plan["taskId"],
                restore_command["arguments"],
                command=restore_command,
                before_release=(
                    resource_candidate["manifest"]["resourceReleaseAfter"]
                    if resource_candidate is not None
                    else manifest["targetGenerations"]["resourceRelease"]
                ),
            )
        self._mutation_may_have_started = True
        if restore_reply.runtime_changed is not True or restore_reply.runtime_revision_after is None:
            raise self._contract_error("module.restore did not publish an authenticated revision transition.", "runtime_apply", None)
        restore_result = restore_value.get("result")
        if not isinstance(restore_result, dict) or restore_result.get("moduleId") != module_id or restore_result.get("moduleGeneration") != manifest["nextGeneration"]:
            raise self._contract_error("module.restore result differs from the prepared module generation.", "runtime_apply", True)
        self._view_generations[module_id] = manifest["targetGenerationsAfter"]["viewGeneration"]
        applied.append("module.restore")
        return applied

    def _invoke_mutation_stage(
        self,
        job_id: str | None,
        operation: str,
        task_id: str,
        arguments: dict[str, Any],
        *,
        command: dict[str, Any] | None = None,
        before_release: str,
    ) -> tuple[dict[str, Any], Any]:
        if self._ledger is None or not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            raise capability_unavailable("composite_runtime_apply", "Durable stage journaling is required before a composite mutation.")
        before_revision = self._runtime_revision
        stage_id = self._operation_id(task_id, before_revision, operation)
        job = self._ledger.get_job(job_id)
        existing = (job.get("result") or {}).get("runtimeStageJournal")
        if not isinstance(existing, list):
            raise CommandError("CONTRACT_MISMATCH", "Composite runtime stage journal is missing.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        if any(isinstance(item, dict) and item.get("stageId") == stage_id for item in existing):
            raise CommandError(
                "STATE_UNKNOWN",
                "Composite stage identity is already journaled; dispatch replay is forbidden.",
                stage="runtime_apply",
                runtime_changed=None,
                recoverable=False,
                details={"freshSessionRequired": True, "automaticReplayAllowed": False},
            )
        self._write_stage_journal(
            job_id,
            {
                "stageId": stage_id,
                "operation": operation,
                "status": "dispatching",
                "beforeObservation": {"runtimeRevision": before_revision, "resourceRelease": before_release},
                "afterObservation": None,
                "runtimeChanged": None,
            },
        )
        try:
            value, reply = self._invoke(operation, task_id, arguments, command=command, mutation=True)
        except Exception as exc:
            changed = getattr(exc, "runtime_changed", None)
            details = getattr(exc, "details", {})
            dispatch_started = isinstance(details, dict) and details.get("dispatchMayHaveStarted") is True
            status = "unknown" if changed is None or dispatch_started else "failed_with_transition" if changed is True else "rejected"
            after_observation = None
            if changed is not None:
                after_observation = {"runtimeRevision": self._runtime_revision}
            error_code = getattr(exc, "code", None)
            self._write_stage_journal(
                job_id,
                {
                    "stageId": stage_id,
                    "operation": operation,
                    "status": status,
                    "beforeObservation": {"runtimeRevision": before_revision, "resourceRelease": before_release},
                    "afterObservation": after_observation,
                    "runtimeChanged": changed,
                    "errorCode": error_code if isinstance(error_code, str) else type(exc).__name__,
                },
                replace_existing=True,
            )
            raise
        status = "acknowledged" if getattr(reply, "runtime_changed", None) is True else "rejected"
        after_observation = {
            "runtimeRevision": getattr(reply, "runtime_revision_after", None),
        }
        self._write_stage_journal(
            job_id,
            {
                "stageId": stage_id,
                "operation": operation,
                "status": status,
                "beforeObservation": {"runtimeRevision": before_revision, "resourceRelease": before_release},
                "afterObservation": after_observation,
                "runtimeChanged": getattr(reply, "runtime_changed", None),
            },
            replace_existing=True,
        )
        return value, reply

    def _write_stage_journal(self, job_id: str, item: dict[str, Any], *, replace_existing: bool = False) -> None:
        assert self._ledger is not None
        job = self._ledger.get_job(job_id)
        result = dict(job.get("result") or {})
        journal = result.get("runtimeStageJournal")
        if not isinstance(journal, list):
            raise CommandError("CONTRACT_MISMATCH", "Composite runtime stage journal is malformed.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        index = next(
            (i for i, prior in enumerate(journal) if isinstance(prior, dict) and prior.get("stageId") == item.get("stageId")),
            None,
        )
        if index is None:
            journal.append(item)
        elif replace_existing and journal[index].get("status") == "dispatching":
            journal[index] = item
        elif journal[index] != item:
            raise CommandError("INPUT_CHANGED", "Durable composite stage journal conflicts with this operation identity.", stage="runtime_apply", runtime_changed=None, recoverable=False)
        result["runtimeStageJournal"] = journal
        self._ledger.update_job(
            job_id,
            state="running",
            stage="runtime_stage_" + str(item.get("operation", "unknown")).replace(".", "_"),
            runtime_changed=None,
            result=result,
        )

    def _observe_plan_state(self, plan: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        module_id = manifest["moduleId"]
        if plan.get("route") == COMPOSITE_ROUTE:
            target = (
                plan["details"]["targetGenerationsAfter"]
                if self._mutation_may_have_started
                else plan["details"]["targetGenerations"]
            )
        else:
            target = manifest["targetGenerationsAfter"] if self._mutation_may_have_started else manifest["targetGenerations"]
        expected_generation = target["moduleGeneration"]
        closure = manifest["dependencyClosure"]
        observed, _ = self._invoke(
            "module.observe", plan["taskId"],
            {"moduleId": module_id, "expectedModuleGeneration": expected_generation, "dependencyClosure": closure},
            mutation=False,
        )
        result = observed.get("result")
        if (
            not isinstance(result, dict)
            or result.get("moduleId") != module_id
            or result.get("moduleGeneration") != expected_generation
            or result.get("stage") != "active"
        ):
            raise CommandError("STATE_UNKNOWN", "Post-apply module identity/generation could not be established.", stage="runtime_reconcile", runtime_changed=None, recoverable=False)
        self._validate_observed_release(result, expected_release=target["resourceRelease"])
        context_value, _ = self._invoke(
            "module.capture_context", plan["taskId"],
            {"moduleId": module_id, "expectedModuleGeneration": expected_generation, "dependencyClosure": closure},
            mutation=False,
        )
        context = context_value.get("result")
        self._validate_context(context, expected_generation)
        state = {
            "sessionId": self._session_id,
            "runtimeRevision": self._runtime_revision,
            "moduleGeneration": expected_generation,
            "resourceRelease": target["resourceRelease"],
            "viewGeneration": context["sourceViewGeneration"],
        }
        self._module_generations[module_id] = expected_generation
        self._view_generations[module_id] = state["viewGeneration"]
        self._last_states[plan["taskId"]] = dict(state)
        self._persist_module_observation(module_id, state)
        return state

    def _latest_module_version(self, module_id: str) -> dict[str, Any] | None:
        if self._ledger is None:
            return None
        return self._ledger.latest_version(self._session_id, "native_module", module_id)

    def _persist_module_observation(self, module_id: str, state: dict[str, Any]) -> None:
        if self._ledger is None:
            return
        latest = self._latest_module_version(module_id)
        generation = state["moduleGeneration"]
        if latest is not None:
            if generation < latest["generation"]:
                raise CommandError(
                    "STALE_TARGET",
                    "Authenticated module generation moved behind the durable Host generation ledger.",
                    stage="runtime_state",
                    runtime_changed=False,
                    recoverable=False,
                )
            if generation == latest["generation"]:
                previous_view = latest.get("metadata", {}).get("viewGeneration")
                if type(previous_view) is int and state["viewGeneration"] < previous_view:
                    raise CommandError(
                        "STALE_TARGET",
                        "Authenticated view generation moved behind the durable Host generation ledger.",
                        stage="runtime_state",
                        runtime_changed=False,
                        recoverable=False,
                    )
                return
        self._ledger.record_version(
            {
                "sessionId": self._session_id,
                "scope": "native_module",
                "subject": module_id,
                "generation": generation,
                "revision": state["runtimeRevision"],
                "state": "observed",
                "metadata": {
                    "resourceRelease": state["resourceRelease"],
                    "viewGeneration": state["viewGeneration"],
                },
            }
        )

    def _reload_candidate(self, plan: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        total = 0
        payloads = []
        for item in manifest["payloads"]:
            dll = self._read_plan_artifact(plan, item["dllArtifactId"], "runtime_reload_dll")
            pdb = b"" if item["pdbArtifactId"] is None else self._read_plan_artifact(plan, item["pdbArtifactId"], "runtime_reload_pdb")
            total += len(dll) + len(pdb)
            if total > MAX_RUNTIME_ARTIFACT_BYTES:
                raise CommandError("CONTRACT_MISMATCH", "Aggregate runtime artifacts exceed 8 MiB.", stage="runtime_apply", runtime_changed=False, recoverable=False)
            payloads.append({
                "name": item["name"],
                "expectedGeneration": item["expectedGeneration"],
                "generationAfter": item["generationAfter"],
                "dllBase64": base64.b64encode(dll).decode("ascii"),
                "dllSha256": "sha256:" + hashlib.sha256(dll).hexdigest(),
                "pdbBase64": base64.b64encode(pdb).decode("ascii") if pdb else "",
                "pdbSha256": "sha256:" + hashlib.sha256(pdb).hexdigest(),
            })
        return {
            "taskId": plan["taskId"],
            "moduleId": manifest["moduleId"],
            "disposedGeneration": manifest["disposedGeneration"],
            "nextGeneration": manifest["nextGeneration"],
            "entryAssemblyName": manifest["entryAssemblyName"],
            "authorizedModuleClosure": manifest["authorizedModuleClosure"],
            "assemblies": manifest["assemblies"],
            "payloads": payloads,
        }

    def _invoke(
        self,
        operation: str,
        task_id: str,
        arguments: dict[str, Any],
        *,
        command: dict[str, Any] | None = None,
        mutation: bool,
    ) -> tuple[dict[str, Any], Any]:
        self._require_configuration("runtime_transport")
        self._assert_connection_identity()
        payload_command = command or self._command(operation, task_id, arguments)
        payload = self._encode_private_command(payload_command)
        request_id = payload_command["requestId"]
        try:
            reply = self._transport.invoke(request_id=request_id, operation=operation, payload=payload)
        except CommandError as exc:
            if exc.runtime_changed is None:
                self._retire_uncertain_session()
            if mutation and (exc.runtime_changed is None or exc.details.get("dispatchMayHaveStarted") is True):
                self._mutation_may_have_started = True
            raise
        if (
            getattr(reply, "request_id", None) != request_id
            or getattr(reply, "schema_id", None) != "relay.liveloop.command-result"
            or getattr(reply, "schema_version", None) != 1
            or getattr(reply, "media_type", None) != "application/json"
        ):
            if mutation:
                self._mutation_may_have_started = True
            raise self._contract_error("Player response schema/request identity differs from the frozen transport contract.", "runtime_transport_result", None)
        try:
            value = _parse_object(reply.payload, "Player response")
        except (ValueError, AttributeError) as exc:
            if mutation:
                self._mutation_may_have_started = True
            raise self._contract_error("Player response is not unique-field UTF-8 JSON.", "runtime_transport_result", None) from exc
        status = value.get("status")
        payload_change = value.get("runtimeChanged")
        if status not in {"completed", "failed", "state_unknown"}:
            raise self._contract_error("Player terminal status is invalid.", "runtime_transport_result", None)
        if payload_change is not None and type(payload_change) is not bool:
            raise self._contract_error("Player runtimeChanged must be boolean or null.", "runtime_transport_result", None)
        if status == "state_unknown" or (status == "failed" and payload_change is None) or (
            "runtimeChanged" in value and payload_change is None
        ):
            if mutation:
                self._mutation_may_have_started = True
            self._retire_uncertain_session()
            raise CommandError(
                "STATE_UNKNOWN",
                "Player payload explicitly reports unknown runtime attribution, contradicting or exceeding the outer transport envelope.",
                stage="runtime_transport_result",
                runtime_changed=None,
                recoverable=False,
                details={
                    "automaticReplayAllowed": False,
                    "freshSessionRequired": True,
                    "outerRuntimeChanged": getattr(reply, "runtime_changed", None),
                    "payloadStatus": status,
                },
            )
        if payload_change is not None and payload_change is not getattr(reply, "runtime_changed", None):
            raise self._contract_error("Player runtimeChanged disagrees with authenticated transport attribution.", "runtime_transport_result", getattr(reply, "runtime_changed", None))
        if getattr(reply, "runtime_changed", None) is None:
            if mutation:
                self._mutation_may_have_started = True
            self._retire_uncertain_session()
            raise CommandError(
                "STATE_UNKNOWN", "Player terminal result leaves runtime attribution unknown; a fresh session is required.",
                stage="runtime_transport_result", runtime_changed=None, recoverable=False,
                details={"automaticReplayAllowed": False, "freshSessionRequired": True},
            )
        changed = reply.runtime_changed
        revision_after = reply.runtime_revision_after
        previous = self._runtime_revision
        if not isinstance(revision_after, str) or not revision_after or (changed is (revision_after == previous)):
            if mutation:
                self._mutation_may_have_started = True
            raise self._contract_error("Authenticated revision transition disagrees with runtimeChanged.", "runtime_transport_result", None)
        if value.get("runtimeRevisionAfter") is not None and value["runtimeRevisionAfter"] != revision_after:
            if mutation:
                self._mutation_may_have_started = True
            raise self._contract_error("Payload runtimeRevisionAfter differs from authenticated transport revision.", "runtime_transport_result", None)
        if changed:
            self._transport.update_expected_runtime_revision(previous, revision_after)
            self._runtime_revision = revision_after
        if mutation and changed:
            self._mutation_may_have_started = True
        if status != "completed":
            error = value.get("error") if isinstance(value.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else ("STATE_UNKNOWN" if status == "state_unknown" else "RUNTIME_APPLY_FAILED")
            raise CommandError(
                code,
                str(error.get("message") or "Player rejected the Native operation."),
                stage=str(error.get("stage") or "runtime_apply"),
                runtime_changed=changed,
                recoverable=error.get("recoverable") is True,
                details=error.get("details") if isinstance(error.get("details"), dict) else {},
            )
        if mutation:
            # A completed mutating call is dispatch evidence even if revision attribution conflicts.
            self._mutation_may_have_started = True
        if not isinstance(value.get("result"), dict):
            if mutation:
                self._mutation_may_have_started = True
            raise self._contract_error("Completed Player result must contain an object result.", "runtime_transport_result", changed)
        if not mutation and changed:
            raise self._contract_error("Read-only module operation changed the runtime revision.", "runtime_transport_result", None)
        return value, reply

    def _retire_uncertain_session(self) -> None:
        self._session_uncertain = True
        retire = getattr(self._transport, "retire_session", None)
        if callable(retire):
            try:
                retire()
            except Exception:
                # The provider-level latch still prevents any follow-up on this identity.
                pass

    def _command(self, operation: str, task_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": 1,
            "requestId": "runtime_" + uuid4().hex,
            "operation": operation,
            "taskId": task_id,
            "context": {"expectedLaunchId": self._launch_id},
            "arguments": arguments,
        }

    @staticmethod
    def _encode_private_command(command: dict[str, Any]) -> bytes:
        try:
            payload = json.dumps(command, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CommandError("INVALID_REQUEST", "Native command is not finite JSON data.", stage="runtime_transport_encode", runtime_changed=False, recoverable=False) from exc
        if len(payload) > MAX_PRIVATE_COMMAND_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Native command exceeds the bounded private payload size.", stage="runtime_transport_encode", runtime_changed=False, recoverable=False)
        return payload

    def _read_task_manifest(self, task: dict[str, Any], descriptor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata, stream = self._open_task_artifact(task["taskId"], descriptor["manifestArtifactId"])
        if metadata["kind"] != MANIFEST_KIND:
            stream.close()
            raise CommandError("CONTRACT_MISMATCH", "manifestArtifactId has the wrong artifact kind.", stage="prepare", runtime_changed=False, recoverable=False)
        try:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            stream.close()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Runtime update manifest exceeds 1 MiB.", stage="prepare", runtime_changed=False, recoverable=False)
        try:
            return _parse_object(raw, "Runtime update manifest"), metadata
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Runtime update manifest is invalid UTF-8 JSON.", stage="prepare", runtime_changed=False, recoverable=False) from exc

    def _read_plan_manifest(self, plan: dict[str, Any]) -> dict[str, Any]:
        artifacts = plan.get("details", {}).get("artifacts", [])
        matches = [item for item in artifacts if isinstance(item, dict) and item.get("kind") == MANIFEST_KIND]
        if len(matches) != 1:
            raise CommandError("CONTRACT_MISMATCH", "Plan must bind exactly one runtime update manifest.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        raw = self._read_plan_artifact(plan, matches[0]["artifactId"], MANIFEST_KIND)
        try:
            return _parse_object(raw, "Runtime update manifest")
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Runtime update manifest is invalid.", stage="runtime_apply", runtime_changed=False, recoverable=False) from exc

    def _read_plan_artifact(self, plan: dict[str, Any], artifact_id: str, kind: str) -> bytes:
        items = [item for item in plan.get("details", {}).get("artifacts", []) if isinstance(item, dict) and item.get("artifactId") == artifact_id]
        if len(items) != 1 or items[0].get("kind") != kind:
            raise CommandError("AUTH_REQUIRED", "Runtime payload is not exactly bound to the immutable plan.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        metadata, stream = self._artifacts.open_verified(artifact_id)
        try:
            if metadata != items[0]:
                raise CommandError("INPUT_CHANGED", "Verified artifact metadata differs from the immutable plan.", stage="runtime_apply", runtime_changed=False, recoverable=False)
            content = stream.read(MAX_RUNTIME_ARTIFACT_BYTES + 1)
        finally:
            stream.close()
        if len(content) > MAX_RUNTIME_ARTIFACT_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Runtime payload exceeds 8 MiB.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        if len(content) != metadata.get("size") or hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
            raise CommandError("INPUT_CHANGED", "Runtime bytes differ from immutable plan metadata.", stage="runtime_apply", runtime_changed=False, recoverable=False)
        return content

    def _read_resource_candidate(self, plan: dict[str, Any]) -> dict[str, Any]:
        try:
            binding = plan["details"]["compositeBinding"]
            resource = binding["resource"]
            state = binding["runtimeState"]
            manifest_bytes = self._read_plan_artifact(plan, resource["manifestArtifactId"], "resource_release_manifest")
            archive_bytes = self._read_plan_artifact(plan, resource["archiveArtifactId"], "resource_release_archive")
            manifest_item = next(
                item for item in plan["details"]["artifacts"] if item["artifactId"] == resource["manifestArtifactId"]
            )
            archive_item = next(
                item for item in plan["details"]["artifacts"] if item["artifactId"] == resource["archiveArtifactId"]
            )
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            archive_sha = hashlib.sha256(archive_bytes).hexdigest()
            parsed = _parse_object(manifest_bytes, "Resource release manifest")
            expected_manifest = {
                "schema": RESOURCE_MANIFEST_SCHEMA,
                "version": 1,
                "taskId": plan["taskId"],
                "sessionId": plan["sessionId"],
                "inputSnapshot": resource["inputSnapshot"],
                "profileDigest": resource["profileDigest"],
                "runtimeRevisionBefore": plan["expectedRuntimeRevision"],
                "resourceReleaseBefore": resource["resourceReleaseBefore"],
                "resourceReleaseAfter": resource["resourceReleaseAfter"],
                "archiveArtifactId": archive_item["artifactId"],
                "archiveSha256": archive_item["sha256"],
                "archiveSize": archive_item["size"],
                "contextRequirements": resource["contextRequirements"],
                "affectedViews": resource["affectedViews"],
            }
            if (
                not isinstance(manifest_bytes, bytes)
                or not 0 < len(manifest_bytes) <= MAX_RESOURCE_MANIFEST_BYTES
                or not 0 < len(archive_bytes) <= MAX_RESOURCE_ARCHIVE_BYTES
                or manifest_item["sha256"] != resource["manifestSha256"]
                or archive_item["sha256"] != resource["archiveSha256"]
                or manifest_sha != resource["manifestSha256"]
                or archive_sha != resource["archiveSha256"]
                or parsed != expected_manifest
                or resource["resourceReleaseBefore"] != state["resourceRelease"]
                or resource["resourceReleaseAfter"] == state["resourceRelease"]
            ):
                raise ValueError("resource manifest/archive differs from the composite binding")
            return {
                "manifest": parsed,
                "manifestBytes": manifest_bytes,
                "manifestSha256": manifest_sha,
                "archiveBytes": archive_bytes,
                "archiveSha256": archive_sha,
            }
        except CommandError:
            raise
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Composite resource manifest/archive binding is invalid.", stage="runtime_apply", runtime_changed=False, recoverable=False) from exc

    def _validate_plan_manifest(
        self,
        plan: dict[str, Any],
        manifest: dict[str, Any],
        *,
        resource_candidate: dict[str, Any] | None = None,
    ) -> None:
        if plan.get("route") == COMPOSITE_ROUTE:
            self._validate_composite_plan(plan, manifest, resource_candidate)
            return
        try:
            normalized, _kinds, payload_ids = self._validate_manifest_fields(
                manifest,
                task_id=plan["taskId"],
                session_id=plan["sessionId"],
                input_snapshot=plan["inputSnapshot"],
                expected_revision=plan["expectedRuntimeRevision"],
                target_generations=plan["details"]["targetGenerations"],
            )
            details = plan["details"]
            for key in ("route", "requiredImpact", "affectedModules", "affectedViews", "expectedRuntimeRevisionAfter", "targetGenerationsAfter"):
                actual = plan["route"] if key == "route" else details.get(key)
                if normalized.get(key) != actual:
                    raise ValueError(f"manifest {key} differs from immutable plan")
            plan_artifacts = details.get("artifacts")
            if not isinstance(plan_artifacts, list) or any(not isinstance(item, dict) for item in plan_artifacts):
                raise ValueError("plan artifact metadata is malformed")
            plan_artifact_ids = [item.get("artifactId") for item in plan_artifacts]
            if (
                any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in plan_artifact_ids)
                or len(set(plan_artifact_ids)) != len(plan_artifact_ids)
            ):
                raise ValueError("plan artifact identities are malformed or duplicated")
            plan_ids = set(plan_artifact_ids)
            manifest_records = [item for item in plan_artifacts if item.get("kind") == MANIFEST_KIND]
            runtime_ids = {*payload_ids, manifest_records[0]["artifactId"]} if len(manifest_records) == 1 else set()
            preparation_evidence = details.get("preparationEvidence")
            receipt_records = [item for item in plan_artifacts if item.get("kind") == "native_compile_input_receipt"]
            expected_plan_ids = runtime_ids
            if preparation_evidence is not None:
                if not isinstance(preparation_evidence, dict):
                    raise ValueError("preparation evidence is malformed")
                validate_compiler_input_coverage(preparation_evidence)
                receipt_id = preparation_evidence.get("compileInputReceiptArtifactId")
                receipt_sha256 = preparation_evidence.get("compileInputReceiptSha256")
                candidate_ids = preparation_evidence.get("editorCandidateArtifactIds")
                if (
                    not isinstance(receipt_id, str)
                    or not ID_RE.fullmatch(receipt_id)
                    or not isinstance(receipt_sha256, str)
                    or not SHA256_RE.fullmatch(receipt_sha256)
                    or not isinstance(candidate_ids, list)
                    or receipt_id not in candidate_ids
                    or len(receipt_records) != 1
                    or receipt_records[0].get("artifactId") != receipt_id
                    or receipt_records[0].get("sha256") != receipt_sha256
                ):
                    raise ValueError("plan compiler receipt is not bound by preparation evidence")
                expected_plan_ids = runtime_ids | {receipt_id}
            elif receipt_records:
                raise ValueError("plan has an unbound compiler receipt artifact")
            if len(manifest_records) != 1 or plan_ids != expected_plan_ids:
                raise ValueError("plan artifact identities differ from the manifest closure")
        except CommandError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Runtime manifest does not match the immutable plan.", stage="runtime_apply", runtime_changed=False, recoverable=False) from exc

    def _validate_composite_plan(
        self,
        plan: dict[str, Any],
        manifest: dict[str, Any],
        resource_candidate: dict[str, Any] | None,
    ) -> None:
        try:
            details = plan["details"]
            binding = details["compositeBinding"]
            if (
                not isinstance(binding, dict)
                or binding.get("schema") != COMPOSITE_BINDING_SCHEMA
                or binding.get("version") != 1
                or binding.get("taskId") != plan["taskId"]
                or binding.get("sessionId") != plan["sessionId"]
                or binding.get("inputSnapshot") != plan["inputSnapshot"]
                or binding.get("runtimeState", {}).get("runtimeRevision") != plan["expectedRuntimeRevision"]
                or resource_candidate is None
            ):
                raise ValueError("composite plan identity/binding is invalid")
            code = binding["code"]
            resource = binding["resource"]
            before = details["targetGenerations"]
            normalized, _kinds, payload_ids = self._validate_manifest_fields(
                manifest,
                task_id=plan["taskId"],
                session_id=plan["sessionId"],
                input_snapshot=code["inputSnapshot"],
                expected_revision=plan["expectedRuntimeRevision"],
                target_generations=before,
            )
            if (
                manifest.get("route") != "MODULE_RELOAD"
                or manifest.get("inputSnapshot") != code["inputSnapshot"]
                or code.get("route") != "MODULE_RELOAD"
                or manifest.get("taskId") != plan["taskId"]
                or manifest.get("sessionId") != plan["sessionId"]
                or normalized["affectedModules"] != details["affectedModules"]
                or code.get("affectedModules") != normalized["affectedModules"]
                or manifest.get("expectedRuntimeRevisionAfter") is not None
                or details.get("expectedRuntimeRevisionAfter") is not None
            ):
                raise ValueError("Native code candidate differs from the composite plan")
            expected_after = {
                "moduleGeneration": before["moduleGeneration"] + 1,
                "resourceRelease": resource["resourceReleaseAfter"],
                "viewGeneration": before["viewGeneration"] + 1,
            }
            if (
                details.get("targetGenerationsAfter") != expected_after
                or normalized["targetGenerations"] != before
                or normalized["targetGenerationsAfter"] != {
                    "moduleGeneration": expected_after["moduleGeneration"],
                    "resourceRelease": before["resourceRelease"],
                    "viewGeneration": expected_after["viewGeneration"],
                }
                or resource["resourceReleaseBefore"] != before["resourceRelease"]
                or resource["resourceReleaseAfter"] != expected_after["resourceRelease"]
                or resource["affectedViews"] != details["affectedViews"]
            ):
                raise ValueError("code/resource generation transitions differ from the immutable plan")
            expected_impact = {
                "hotfix": False,
                "rebuildViews": resource["affectedViews"],
                "reloadModules": normalized["requiredImpact"]["reloadModules"],
                "restartPlayer": False,
                "buildBaseline": False,
            }
            if details.get("requiredImpact") != expected_impact or normalized["requiredImpact"]["reloadModules"] != details["affectedModules"]:
                raise ValueError("composite impact differs from the Native and resource closures")

            plan_artifacts = details.get("artifacts")
            if not isinstance(plan_artifacts, list) or any(not isinstance(item, dict) for item in plan_artifacts):
                raise ValueError("composite artifact metadata is malformed")
            actual_by_id = {item.get("artifactId"): item for item in plan_artifacts}
            if len(actual_by_id) != len(plan_artifacts):
                raise ValueError("composite artifact identities are duplicated")
            expected_ids: set[str] = set()
            for component in (code, resource):
                component_artifacts = component.get("artifacts")
                if (
                    not isinstance(component_artifacts, list)
                    or set(component.get("artifactIds", [])) != {item.get("artifactId") for item in component_artifacts}
                    or component.get("artifactClosureSha256") != artifact_closure_sha256(component_artifacts)
                ):
                    raise ValueError("component closure digest or IDs differ")
                expected_ids.update(component["artifactIds"])
                for item in component_artifacts:
                    if actual_by_id.get(item.get("artifactId")) != item:
                        raise ValueError("component artifact metadata differs from plan metadata")
                    metadata, stream = self._artifacts.open_verified(item["artifactId"])
                    stream.close()
                    if metadata != item:
                        raise ValueError("registered immutable artifact differs from its plan record")
            if set(actual_by_id) != expected_ids:
                raise ValueError("plan artifact set is not exactly the joint code/resource closure")
            if (
                code.get("manifestArtifactId") not in code["artifactIds"]
                or code.get("manifestSha256") != actual_by_id[code["manifestArtifactId"]]["sha256"]
                or resource.get("manifestArtifactId") not in resource["artifactIds"]
                or resource.get("archiveArtifactId") not in resource["artifactIds"]
            ):
                raise ValueError("code/resource manifest or archive identity is not closed by the plan")
            evidence = details.get("preparationEvidence")
            if (
                not isinstance(evidence, dict)
                or evidence.get("schema") != "relay.liveloop.composite-preparation-evidence"
                or evidence.get("code", {}).get("runtimeManifestArtifactId") != code["manifestArtifactId"]
                or evidence.get("resource") != resource.get("editorEvidence")
            ):
                raise ValueError("composite preparation evidence differs from the candidate binding")
            code_evidence = evidence["code"]
            validate_compiler_input_coverage(code_evidence)
            receipt_id = code_evidence.get("compileInputReceiptArtifactId")
            receipt_sha256 = code_evidence.get("compileInputReceiptSha256")
            candidate_ids = code_evidence.get("editorCandidateArtifactIds")
            receipt_records = [item for item in code["artifacts"] if item.get("artifactId") == receipt_id]
            if (
                not isinstance(receipt_id, str)
                or not ID_RE.fullmatch(receipt_id)
                or not isinstance(receipt_sha256, str)
                or not SHA256_RE.fullmatch(receipt_sha256)
                or not isinstance(candidate_ids, list)
                or receipt_id not in candidate_ids
                or len(receipt_records) != 1
                or receipt_records[0].get("kind") != "native_compile_input_receipt"
                or receipt_records[0].get("sha256") != receipt_sha256
            ):
                raise ValueError("composite compiler input limitations are not bound to the code receipt")
            manifest_records = [item for item in code["artifacts"] if item.get("artifactId") == code["manifestArtifactId"]]
            if len(manifest_records) != 1 or manifest_records[0].get("kind") != MANIFEST_KIND:
                raise ValueError("Native manifest is not uniquely bound in the code closure")
            if not set(payload_ids).issubset(set(code["artifactIds"])):
                raise ValueError("Native payload closure extends beyond the code artifact set")
        except CommandError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Composite runtime plan differs from its immutable code/resource binding.", stage="runtime_apply", runtime_changed=False, recoverable=False) from exc

    def _validate_manifest_fields(
        self,
        manifest: dict[str, Any],
        *,
        task_id: str,
        session_id: str,
        input_snapshot: str,
        expected_revision: str,
        target_generations: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        common = {
            "schema", "version", "taskId", "sessionId", "launchId", "inputSnapshot", "route", "moduleId",
            "dependencyClosure", "expectedRuntimeRevision", "expectedRuntimeRevisionAfter", "targetGenerations",
            "targetGenerationsAfter", "requiredImpact", "affectedModules", "affectedViews",
        }
        route = manifest.get("route")
        hotfix_fields = {"assemblyArtifactId", "assemblyName", "expectedAssemblyGeneration", "types"}
        module_fields = {
            "resourceRelease", "disposedGeneration", "nextGeneration", "entryAssemblyName", "authorizedModuleClosure",
            "assemblies", "payloads",
        }
        fields = common | (hotfix_fields if route == "HOTFIX" else module_fields if route == "MODULE_RELOAD" else set())
        if set(manifest) != fields:
            raise ValueError("manifest fields do not match its route")
        if manifest["schema"] != MANIFEST_SCHEMA or type(manifest["version"]) is not int or manifest["version"] != 1:
            raise ValueError("manifest schema/version is unsupported")
        if route not in SUPPORTED_ROUTES:
            raise ValueError("route is unsupported")
        for key, expected in (
            ("taskId", task_id), ("sessionId", session_id), ("launchId", self._launch_id),
            ("inputSnapshot", input_snapshot), ("expectedRuntimeRevision", expected_revision),
        ):
            if _canonical_text(manifest[key], f"manifest.{key}") != expected:
                raise ValueError(f"manifest {key} differs from the task/session/plan")
        module_id = _canonical_text(manifest["moduleId"], "moduleId", 128)
        if not ID_RE.fullmatch(module_id):
            raise ValueError("moduleId is not a canonical identifier")
        closure = _string_array(manifest["dependencyClosure"], "dependencyClosure", nonempty=True)
        if module_id not in closure:
            raise ValueError("dependencyClosure omits moduleId")
        before = _generations(target_generations, "plan.targetGenerations")
        manifest_before = _generations(manifest["targetGenerations"], "manifest.targetGenerations")
        after = _generations(manifest["targetGenerationsAfter"], "manifest.targetGenerationsAfter")
        if manifest_before != before or self._resource_release_id is None:
            raise ValueError("manifest target generations differ from the authenticated state")
        required_impact = _impact(manifest["requiredImpact"])
        affected_modules = _string_array(manifest["affectedModules"], "affectedModules")
        affected_views = _string_array(manifest["affectedViews"], "affectedViews")
        if affected_views or required_impact["rebuildViews"] or required_impact["restartPlayer"] or required_impact["buildBaseline"]:
            raise ValueError("Native runtime routes cannot rebuild views, restart, or build a baseline")
        if route == "HOTFIX":
            artifact_id = manifest["assemblyArtifactId"]
            if not isinstance(artifact_id, str) or not ID_RE.fullmatch(artifact_id):
                raise ValueError("assemblyArtifactId is invalid")
            assembly_name = _canonical_text(manifest["assemblyName"], "assemblyName")
            if module_id not in affected_modules or not required_impact["hotfix"] or required_impact["reloadModules"]:
                raise ValueError("hotfix route is not covered by the task impact")
            generation = _generation(manifest["expectedAssemblyGeneration"], "expectedAssemblyGeneration")
            if generation != before["moduleGeneration"] or after != before:
                raise ValueError("hotfix must target the current generation and preserve generations")
            types = manifest["types"]
            if not isinstance(types, list) or not types or any(
                not isinstance(item, dict) or set(item) != {"typeName", "signatures"}
                or not isinstance(item["typeName"], str) or not item["typeName"]
                or not isinstance(item["signatures"], list) or not item["signatures"]
                or any(not isinstance(sig, str) or not sig for sig in item["signatures"])
                for item in types
            ):
                raise ValueError("hotfix types are malformed")
            expected_after = runtime_revision_after(expected_revision, task_id, "hotfix")
            if manifest["expectedRuntimeRevisionAfter"] != expected_after:
                raise ValueError("hotfix runtime revision transition is not deterministic")
            payload_ids = [artifact_id]
            kinds = [MANIFEST_KIND, "runtime_hotfix_assembly"]
        else:
            if required_impact["hotfix"] or not required_impact["reloadModules"]:
                raise ValueError("module route is not covered by reloadModules impact")
            if set(affected_modules) != set(required_impact["reloadModules"]) or set(affected_modules) != set(closure):
                raise ValueError("module dependency closure differs from the authorized task impact")
            authorized_closure = _string_array(
                manifest["authorizedModuleClosure"], "authorizedModuleClosure", nonempty=True
            )
            if set(authorized_closure) != set(closure) or module_id not in authorized_closure:
                raise ValueError(
                    "authorizedModuleClosure must exactly cover dependencyClosure and include moduleId"
                )
            if manifest["resourceRelease"] != before["resourceRelease"]:
                raise ValueError("module reload cannot change resourceRelease")
            disposed = _generation(manifest["disposedGeneration"], "disposedGeneration")
            next_generation = _generation(manifest["nextGeneration"], "nextGeneration")
            if disposed != before["moduleGeneration"] or next_generation <= disposed:
                raise ValueError("module generation does not advance from the observed generation")
            if after != {
                "moduleGeneration": next_generation,
                "resourceRelease": before["resourceRelease"],
                "viewGeneration": before["viewGeneration"] + 1,
            }:
                raise ValueError("module generation/view transition is not the frozen +1 transition")
            if manifest["expectedRuntimeRevisionAfter"] is not None:
                raise ValueError("module route revisionAfter must be Player-assigned (null in the prepared plan)")
            assemblies = manifest["assemblies"]
            payloads = manifest["payloads"]
            if not isinstance(assemblies, list) or not assemblies or not isinstance(payloads, list) or not payloads or len(assemblies) != len(payloads):
                raise ValueError("module assembly/payload closure is empty or incomplete")
            names: set[str] = set()
            dependencies_by_name: dict[str, list[str]] = {}
            for assembly in assemblies:
                _object(assembly, {"name", "dependencies"}, "assemblies[]")
                name = _canonical_text(assembly["name"], "assembly.name")
                dependencies = _string_array(assembly["dependencies"], "assembly.dependencies")
                if name in names:
                    raise ValueError("assembly names are duplicated")
                names.add(name)
                if name in dependencies:
                    raise ValueError("assembly dependency closure contains a self-reference")
                dependencies_by_name[name] = dependencies
            if any(not set(dependencies).issubset(names) for dependencies in dependencies_by_name.values()):
                raise ValueError("assembly dependency closure references an assembly outside the reload payloads")
            entry_name = _canonical_text(manifest["entryAssemblyName"], "entryAssemblyName")
            if entry_name not in names:
                raise ValueError("entryAssemblyName is outside the assembly closure")
            payload_ids = []
            payload_names: set[str] = set()
            for payload in payloads:
                _object(payload, {"name", "expectedGeneration", "generationAfter", "dllArtifactId", "pdbArtifactId"}, "payloads[]")
                name = _canonical_text(payload["name"], "payload.name")
                if name in payload_names or _generation(payload["expectedGeneration"], "expectedGeneration") != disposed or _generation(payload["generationAfter"], "generationAfter") != next_generation:
                    raise ValueError("payload identities/generations do not match the module transition")
                payload_names.add(name)
                for key in ("dllArtifactId", "pdbArtifactId"):
                    artifact_id = payload[key]
                    if artifact_id is None and key == "pdbArtifactId":
                        continue
                    if not isinstance(artifact_id, str) or not ID_RE.fullmatch(artifact_id):
                        raise ValueError(f"payload {key} is invalid")
                    payload_ids.append(artifact_id)
            if payload_names != names or len(set(payload_ids)) != len(payload_ids):
                raise ValueError("payload closure does not exactly match assemblies")
            expected_after = None
            kinds = [MANIFEST_KIND, "runtime_reload_dll"]
            if any(payload["pdbArtifactId"] is not None for payload in payloads):
                kinds.append("runtime_reload_pdb")
        normalized = {
            "route": route,
            "requiredImpact": required_impact,
            "affectedModules": affected_modules,
            "affectedViews": affected_views,
            "expectedRuntimeRevisionAfter": expected_after,
            "targetGenerations": before,
            "targetGenerationsAfter": after,
        }
        return normalized, kinds, payload_ids

    def _validate_manifest_identity(self, manifest: dict[str, Any], task: dict[str, Any], descriptor: dict[str, Any]) -> None:
        try:
            if (
                manifest.get("taskId") != task["taskId"]
                or manifest.get("sessionId") != task["sessionId"]
                or manifest.get("launchId") != self._launch_id
                or manifest.get("inputSnapshot") != descriptor["inputSnapshot"]
            ):
                raise ValueError("manifest identity differs from the active task/session")
            if self._resource_release_id is None:
                raise ValueError("resourceReleaseId is not configured")
            _canonical_text(manifest.get("moduleId"), "moduleId", 128)
            _string_array(manifest.get("dependencyClosure"), "dependencyClosure", nonempty=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError("WRONG_SESSION", "Task manifest identity does not match the authenticated Player session.", stage="runtime_state", runtime_changed=False, recoverable=False) from exc

    def _module_id(self, manifest: dict[str, Any]) -> str:
        module_id = manifest.get("moduleId")
        if not isinstance(module_id, str) or not ID_RE.fullmatch(module_id):
            raise CommandError("CONTRACT_MISMATCH", "Manifest moduleId is invalid.", stage="runtime_state", runtime_changed=False, recoverable=False)
        return module_id

    def _validate_observed_release(self, result: dict[str, Any], *, expected_release: str | None = None) -> None:
        expected = self._resource_release_id if expected_release is None else expected_release
        if expected is None:
            raise capability_unavailable("runtime_apply", "resourceReleaseId is required in the protected runtime session.")
        state = result.get("state")
        if isinstance(state, dict) and "resourceRelease" in state and state["resourceRelease"] != expected:
            raise CommandError("STALE_TARGET", "Observed module resourceRelease differs from the protected runtime session.", stage="runtime_state", runtime_changed=False, recoverable=False)

    def _validate_context(self, context: Any, module_generation: int) -> None:
        if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS:
            raise CommandError("CONTRACT_MISMATCH", "module.capture_context result fields do not match the frozen context contract.", stage="runtime_state", runtime_changed=False, recoverable=False)
        if (
            not isinstance(context["schemaId"], str) or not context["schemaId"]
            or type(context["schemaVersion"]) is not int or context["schemaVersion"] <= 0
            or not isinstance(context["mediaType"], str) or not context["mediaType"]
            or context["sourceSessionId"] != self._session_id
            or type(context["sourceModuleGeneration"]) is not int
            or context["sourceModuleGeneration"] != module_generation
            or type(context["sourceViewGeneration"]) is not int or context["sourceViewGeneration"] < 0
            or not isinstance(context["dataRevision"], str) or not context["dataRevision"]
            or not isinstance(context["payloadBase64"], str)
        ):
            raise CommandError("WRONG_SESSION", "Captured context identity/generation is invalid.", stage="runtime_state", runtime_changed=False, recoverable=False)
        try:
            payload = base64.b64decode(context["payloadBase64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CommandError("CONTRACT_MISMATCH", "Captured context payload is not canonical Base64.", stage="runtime_state", runtime_changed=False, recoverable=False) from exc
        if base64.b64encode(payload).decode("ascii") != context["payloadBase64"] or len(payload) > 64 * 1024:
            raise CommandError("CONTRACT_MISMATCH", "Captured context payload exceeds 64 KiB or is non-canonical Base64.", stage="runtime_state", runtime_changed=False, recoverable=False)

    def _assert_connection_identity(self) -> dict[str, Any]:
        state = self._transport.connection_state()
        if not isinstance(state, dict) or state.get("transportAuthenticated") is not True:
            raise capability_unavailable("runtime_apply", "No authenticated Player connection is available.")
        if state.get("sessionId") != self._session_id or state.get("launchId") != self._launch_id:
            raise CommandError("WRONG_SESSION", "Authenticated Player session/launch differs from the protected runtime session.", stage="runtime_transport_identity", runtime_changed=False, recoverable=False)
        if state.get("expectedRuntimeRevision") != self._runtime_revision:
            raise CommandError("STALE_TARGET", "Authenticated Player revision differs from the Host's last authenticated revision.", stage="runtime_transport_identity", runtime_changed=False, recoverable=False)
        return state

    def _require_configuration(self, stage: str) -> None:
        if self._session_uncertain:
            raise CommandError(
                "STATE_UNKNOWN",
                "This Player session has already reported or implied unknown runtime state; a fresh session is required.",
                stage=stage,
                runtime_changed=None,
                recoverable=False,
                details={"freshSessionRequired": True, "automaticReplayAllowed": False},
            )
        if self._resource_release_id is None:
            raise capability_unavailable("runtime_apply", "resourceReleaseId is required in the protected runtime session.")
        self._assert_connection_identity()

    def _open_task_artifact(self, task_id: str, artifact_id: str) -> tuple[dict[str, Any], Any]:
        try:
            record = self._artifacts.ledger.get_artifact(artifact_id)
            if record.get("taskId") != task_id:
                raise CommandError("AUTH_REQUIRED", "Runtime artifact is not registered to this task.", stage="prepare", runtime_changed=False, recoverable=False)
            return self._artifacts.open_verified(artifact_id)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError("INPUT_CHANGED", "Task-bound runtime artifact could not be verified.", stage="prepare", runtime_changed=False, recoverable=False) from exc

    def _operation_id(self, task_id: str, previous: str, stage: str) -> str:
        material = "\n".join(("relay.liveloop.native-operation/1", task_id, previous, stage)).encode("utf-8")
        return "native:" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _impact_subset(candidate: dict[str, Any], boundary: dict[str, Any]) -> bool:
        return (
            all(not candidate[key] or boundary[key] for key in ("hotfix", "restartPlayer", "buildBaseline"))
            and set(candidate["rebuildViews"]).issubset(boundary["rebuildViews"])
            and set(candidate["reloadModules"]).issubset(boundary["reloadModules"])
        )

    @staticmethod
    def _state_unknown(message: str, revision: str | None, steps: list[str]) -> dict[str, Any]:
        return {
            "status": "state_unknown",
            "runtimeChanged": None,
            "runtimeRevisionAfter": revision,
            "facts": {"runtimeMatched": None},
            "appliedSteps": steps,
            "methodStates": [],
            "error": {
                "code": "STATE_UNKNOWN", "stage": "runtime_reconcile", "message": message,
                "recoverable": False,
                "details": {"automaticReplayAllowed": False, "freshSessionRequired": True},
            },
        }

    @staticmethod
    def _contract_error(message: str, stage: str, runtime_changed: bool | None = False) -> CommandError:
        return CommandError("CONTRACT_MISMATCH", message, stage=stage, runtime_changed=runtime_changed, recoverable=False)


class ManifestPreparationProvider:
    """Prepare from task-bound immutable artifacts; this provider does not compile or build."""

    provider_id = "registered-native-manifest-preparation"

    def __init__(self, artifacts: Any, runtime_provider: PlayerRuntimeUpdateProvider) -> None:
        self._artifacts = artifacts
        self._runtime_provider = runtime_provider

    @property
    def is_verified(self) -> bool:
        return self._runtime_provider.is_verified

    @property
    def unverified_reason(self) -> str | None:
        return self._runtime_provider.unverified_reason

    def probe_capability(self, capability: str | None = None) -> bool:
        return self._runtime_provider.probe_capability(capability)

    def current_input_snapshot(self, task: dict[str, Any]) -> str:
        return _descriptor(task.get("reference"))["inputSnapshot"]

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        descriptor = _descriptor(task.get("reference"))
        if request.get("requestedInputSnapshot") not in {None, descriptor["inputSnapshot"]}:
            raise CommandError("INPUT_CHANGED", "Requested input snapshot differs from the task-bound manifest.", stage="prepare", runtime_changed=False)
        state = self._runtime_provider.observe_task_state(task)
        if request.get("expectedRuntimeRevision") not in {None, state["runtimeRevision"]}:
            raise CommandError("STALE_TARGET", "Requested runtime revision differs from authenticated state.", stage="prepare", runtime_changed=False)
        metadata, stream = self._open_task_artifact(task["taskId"], descriptor["manifestArtifactId"])
        if metadata["kind"] != MANIFEST_KIND:
            stream.close()
            raise CommandError("CONTRACT_MISMATCH", "manifestArtifactId has the wrong artifact kind.", stage="prepare", runtime_changed=False, recoverable=False)
        try:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            stream.close()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CommandError("CONTRACT_MISMATCH", "Runtime update manifest exceeds 1 MiB.", stage="prepare", runtime_changed=False, recoverable=False)
        try:
            manifest = _parse_object(raw, "Runtime update manifest")
        except ValueError as exc:
            raise CommandError("CONTRACT_MISMATCH", "Runtime update manifest is invalid UTF-8 JSON.", stage="prepare", runtime_changed=False, recoverable=False) from exc
        normalized, kinds = self._runtime_provider.prepare_manifest(task, descriptor, manifest, state)
        artifacts = []
        for artifact_id in descriptor["artifactIds"]:
            artifact_metadata, artifact_stream = self._open_task_artifact(task["taskId"], artifact_id)
            artifact_stream.close()
            artifacts.append(artifact_metadata)
        if not set(kinds).issubset({item["kind"] for item in artifacts}):
            raise CommandError("RESOURCE_BUILD_FAILED", "Registered artifact closure is missing a required Native payload kind.", stage="prepare", runtime_changed=False)
        return {
            "inputSnapshot": descriptor["inputSnapshot"],
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
        }

    def reconcile_prepare(self, job: dict[str, Any], task: dict[str, Any]) -> dict[str, Any] | None:
        del job, task
        return None

    def _open_task_artifact(self, task_id: str, artifact_id: str) -> tuple[dict[str, Any], Any]:
        try:
            record = self._artifacts.ledger.get_artifact(artifact_id)
            if record.get("taskId") != task_id:
                raise CommandError("AUTH_REQUIRED", "Runtime artifact is not registered to this task.", stage="prepare", runtime_changed=False, recoverable=False)
            return self._artifacts.open_verified(artifact_id)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError("INPUT_CHANGED", "Task-bound Native artifact could not be verified.", stage="prepare", runtime_changed=False, recoverable=False) from exc


def create_native_update_providers(
    transport: Any,
    artifacts: Any,
    session: Any,
    *,
    ledger: Any | None = None,
    profiles: Any | None = None,
    editor_transport: Any | None = None,
    resource_provider: Any | None = None,
) -> tuple[Any, PlayerRuntimeUpdateProvider]:
    if (profiles is None) != (editor_transport is None):
        raise ValueError("profiles and editor_transport must be configured together")
    if profiles is not None and ledger is None:
        raise ValueError("durable Host ledger is required for source-compile preparation")
    runtime = PlayerRuntimeUpdateProvider(
        transport,
        artifacts,
        session_id=session.session_id,
        launch_id=session.launch_id,
        runtime_revision=session.runtime_revision,
        resource_release_id=getattr(session, "resource_release_id", None),
        profile_resolver=profiles,
        ledger=ledger,
    )
    if profiles is None:
        if resource_provider is not None:
            raise ValueError("resource_provider requires profile-backed Native source preparation")
        return ManifestPreparationProvider(artifacts, runtime), runtime
    from .native_compile_provider import NativeSourcePreparationProvider

    code_provider = NativeSourcePreparationProvider(ledger, artifacts, editor_transport, profiles, runtime)
    if resource_provider is None:
        return code_provider, runtime
    if ledger is None:
        raise ValueError("resource_provider requires the durable Host ledger")
    from .composite_update import CompositePreparationProvider

    return CompositePreparationProvider(code_provider, resource_provider, artifacts, ledger), runtime
