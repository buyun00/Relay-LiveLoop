from __future__ import annotations

import base64
import hashlib
import json
import argparse
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import relay_liveloop
from host.artifacts import ArtifactStore
from host.errors import CommandError
from host.ledger import Ledger
from host.runtime_session import RuntimeSessionConfig
from host.runtime_transport import LoopbackRuntimeHostTransport, RuntimeTransportReply
from host.runtime_update_provider import (
    MANIFEST_KIND,
    MANIFEST_SCHEMA,
    PREPARATION_SCHEMA,
    canonical_input_sha256,
    create_native_update_providers,
    runtime_revision_after,
)
from host.validation import OPERATIONS
from relay_liveloop import build_command_service


SESSION_ID = "session-synthetic"
LAUNCH_ID = "launch-synthetic"
RESOURCE_RELEASE = "release-synthetic-1"
MODULE_ID = "Synthetic.Module"


def _impact(route: str) -> dict[str, Any]:
    return {
        "hotfix": route == "HOTFIX",
        "rebuildViews": [],
        "reloadModules": [MODULE_ID] if route in {"MODULE_RELOAD", "MODULE_AND_ASSET_RELOAD"} else [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


def _transition(previous: str, stage_number: int) -> str:
    # The fake Player assigns each authenticated module-stage revision independently.
    return f"{previous}:synthetic-{stage_number}"


class FakeAuthenticatedPlayerTransport:
    """Substitute only the authenticated I/O edge; production composition remains intact."""

    def __init__(self) -> None:
        self.authenticated = True
        self.session_id = SESSION_ID
        self.launch_id = LAUNCH_ID
        self.expected_revision = "revision-synthetic-0"
        self.runtime_revision = self.expected_revision
        self.module_generation = 2
        self.view_generation = 4
        self.resource_release = RESOURCE_RELEASE
        self.native_ready = True
        self.missing_module_id = False
        self.wrong_module_id = False
        self.mutate_payload_on_capture: Path | None = None
        self.lose_on_operation: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.mutations: list[str] = []
        self._revision_counter = 0

    def connection_state(self) -> dict[str, Any]:
        return {
            "transportAuthenticated": self.authenticated,
            "sessionId": self.session_id if self.authenticated else None,
            "launchId": self.launch_id if self.authenticated else None,
            "expectedRuntimeRevision": self.expected_revision,
            "nativeCapabilitiesVerified": False,
        }

    def update_expected_runtime_revision(self, previous: str, current: str) -> None:
        if self.expected_revision != previous:
            raise CommandError("STALE_TARGET", "fake transport expected revision changed", stage="test", runtime_changed=False)
        self.expected_revision = current

    def invoke(self, *, request_id: str, operation: str, payload: bytes, timeout_seconds=None, expected_context=None) -> RuntimeTransportReply:
        if not self.authenticated:
            raise CommandError("CAPABILITY_UNAVAILABLE", "fake Player is disconnected", stage="test", runtime_changed=False)
        command = json.loads(payload.decode("utf-8"))
        if command["requestId"] != request_id or command["operation"] != operation:
            raise AssertionError("invoke identity differs from inner command")
        if command["context"] != {"expectedLaunchId": self.launch_id}:
            raise AssertionError("private command context differs from the frozen wire contract")
        if not isinstance(command.get("taskId"), str) or not command["taskId"]:
            raise AssertionError("private operation must retain its caller taskId")
        self.calls.append({"operation": operation, "command": command})
        if operation == "module.observe":
            args = command["arguments"]
            if set(args) != {"moduleId", "expectedModuleGeneration", "dependencyClosure"}:
                raise AssertionError("module.observe arguments differ from the frozen wire contract")
            module_id = "Different.Module" if self.wrong_module_id else args["moduleId"]
            result: dict[str, Any] = {
                "moduleId": module_id,
                "moduleGeneration": self.module_generation,
                "stage": "active",
                "state": {"resourceRelease": self.resource_release},
            }
            if self.missing_module_id:
                result.pop("moduleId")
            return self._reply(request_id, operation, result, native_ready=self.native_ready)
        if operation == "module.capture_context":
            args = command["arguments"]
            if set(args) != {"moduleId", "expectedModuleGeneration", "dependencyClosure"}:
                raise AssertionError("module.capture_context arguments differ from the frozen wire contract")
            if self.mutate_payload_on_capture is not None:
                self.mutate_payload_on_capture.write_bytes(b"changed-after-coordinator-verification")
                self.mutate_payload_on_capture = None
            return self._reply(
                request_id,
                operation,
                {
                    "schemaId": "synthetic.context",
                    "schemaVersion": 1,
                    "mediaType": "application/json",
                    "sourceSessionId": self.session_id,
                    "sourceModuleGeneration": self.module_generation,
                    "sourceViewGeneration": self.view_generation,
                    "dataRevision": "context-synthetic-1",
                    "payloadBase64": base64.b64encode(b"{}").decode("ascii"),
                },
            )
        if operation == "hotfix":
            args = command["arguments"]
            expected = {
                "inputSha256", "runtimeOperationId", "runtimeRevisionAfter", "assemblyName",
                "expectedAssemblyGeneration", "assemblyBase64", "assemblySha256", "types",
            }
            if set(args) != expected:
                raise AssertionError("hotfix arguments differ from the frozen wire contract")
            if args["inputSha256"] != canonical_input_sha256(command):
                raise AssertionError("hotfix normalized payload digest mismatch")
            assembly = base64.b64decode(args["assemblyBase64"], validate=True)
            digest = "sha256:" + hashlib.sha256(assembly).hexdigest()
            if digest != args["assemblySha256"]:
                raise AssertionError("hotfix assembly byte digest mismatch")
            self.mutations.append(operation)
            self.runtime_revision = args["runtimeRevisionAfter"]
            return self._reply(
                request_id,
                operation,
                {
                    "assemblyName": args["assemblyName"],
                    "assemblyGeneration": args["expectedAssemblyGeneration"],
                    "appliedSha256": digest,
                },
                changed=True,
            )
        if operation == "module.quiesce":
            self._check_standard_module_args(command)
            return self._mutating_reply(request_id, operation, {})
        if operation == "module.dispose":
            self._check_standard_module_args(command)
            return self._mutating_reply(request_id, operation, {})
        if operation == "module.load":
            args = command["arguments"]
            if set(args) != {"moduleId", "expectedModuleGeneration", "dependencyClosure", "candidate"}:
                raise AssertionError("module.load arguments differ from the frozen wire contract")
            candidate = args["candidate"]
            if set(candidate) != {
                "taskId", "inputSha256", "moduleId", "disposedGeneration", "nextGeneration",
                "entryAssemblyName", "authorizedModuleClosure", "assemblies", "payloads",
            }:
                raise AssertionError("module.load candidate differs from the frozen wire contract")
            if candidate["inputSha256"] != canonical_input_sha256(command):
                raise AssertionError("module.load normalized payload digest mismatch")
            if candidate["taskId"] != command["taskId"]:
                raise AssertionError("module.load task lineage changed")
            for item in candidate["payloads"]:
                dll = base64.b64decode(item["dllBase64"], validate=True)
                if "sha256:" + hashlib.sha256(dll).hexdigest() != item["dllSha256"]:
                    raise AssertionError("module.load DLL byte digest mismatch")
                pdb = base64.b64decode(item["pdbBase64"], validate=True) if item["pdbBase64"] else b""
                if "sha256:" + hashlib.sha256(pdb).hexdigest() != item["pdbSha256"]:
                    raise AssertionError("module.load PDB byte digest mismatch")
            self.module_generation = candidate["nextGeneration"]
            return self._mutating_reply(
                request_id,
                operation,
                {"moduleId": candidate["moduleId"], "moduleGeneration": candidate["nextGeneration"]},
                lose_after_effect=True,
            )
        if operation == "resource.activate":
            args = command["arguments"]
            expected = {
                "inputSha256", "moduleId", "expectedModuleGeneration", "dependencyClosure",
                "resourceReleaseBefore", "resourceReleaseAfter", "manifestBase64", "manifestSha256",
                "archiveBase64", "archiveSha256", "contextRequirements",
            }
            if set(args) != expected or args["inputSha256"] != canonical_input_sha256(command):
                raise AssertionError("resource.activate arguments or normalized digest differ from the neutral contract")
            manifest_raw = base64.b64decode(args["manifestBase64"], validate=True)
            archive = base64.b64decode(args["archiveBase64"], validate=True)
            manifest = json.loads(manifest_raw.decode("utf-8"))
            if (
                "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != args["manifestSha256"]
                or "sha256:" + hashlib.sha256(archive).hexdigest() != args["archiveSha256"]
                or manifest["resourceReleaseBefore"] != self.resource_release
                or manifest["resourceReleaseAfter"] != args["resourceReleaseAfter"]
                or manifest["archiveSha256"] != hashlib.sha256(archive).hexdigest()
            ):
                raise AssertionError("resource.activate bytes differ from the authenticated composite candidate")
            self.resource_release = args["resourceReleaseAfter"]
            return self._mutating_reply(
                request_id,
                operation,
                {
                    "moduleId": args["moduleId"],
                    "moduleGeneration": args["expectedModuleGeneration"],
                    "resourceRelease": self.resource_release,
                    "archiveSha256": args["archiveSha256"],
                    "manifestSha256": args["manifestSha256"],
                },
            )
        if operation == "module.restore":
            args = command["arguments"]
            if set(args) != {"moduleId", "expectedModuleGeneration", "dependencyClosure", "context"}:
                raise AssertionError("module.restore arguments differ from the frozen wire contract")
            if set(args["context"]) != {
                "schemaId", "schemaVersion", "mediaType", "sourceSessionId", "sourceModuleGeneration",
                "sourceViewGeneration", "dataRevision", "payloadBase64",
            }:
                raise AssertionError("module.restore context differs from the frozen context contract")
            self.view_generation += 1
            return self._mutating_reply(
                request_id,
                operation,
                {"moduleId": args["moduleId"], "moduleGeneration": self.module_generation},
            )
        if operation == "module.reconcile":
            if set(command["arguments"]) != {"moduleId"}:
                raise AssertionError("module.reconcile arguments differ from the frozen wire contract")
            return self._reply(request_id, operation, {"newSessionRequired": True})
        raise AssertionError(f"unexpected private operation: {operation}")

    def _check_standard_module_args(self, command: dict[str, Any]) -> None:
        args = command["arguments"]
        if set(args) != {"moduleId", "expectedModuleGeneration", "dependencyClosure"}:
            raise AssertionError("standard module arguments differ from the frozen wire contract")

    def _mutating_reply(self, request_id: str, operation: str, result: dict[str, Any], *, lose_after_effect: bool = False) -> RuntimeTransportReply:
        self.mutations.append(operation)
        self._revision_counter += 1
        self.runtime_revision = _transition(self.runtime_revision, self._revision_counter)
        if self.lose_on_operation == operation or (lose_after_effect and self.lose_on_operation == "module.load"):
            self.authenticated = False
            raise CommandError(
                "STATE_UNKNOWN",
                "synthetic terminal response lost after dispatch",
                stage="runtime_transport_wait",
                runtime_changed=None,
                recoverable=False,
                details={"dispatchMayHaveStarted": True, "automaticReplayAllowed": False},
            )
        return self._reply(request_id, operation, result, changed=True)

    def _reply(self, request_id: str, operation: str, result: dict[str, Any], *, changed: bool = False, native_ready: bool | None = None) -> RuntimeTransportReply:
        del operation
        value: dict[str, Any] = {
            "status": "completed",
            "runtimeChanged": changed,
            "runtimeRevisionAfter": self.runtime_revision,
            "result": result,
            "error": None,
        }
        if native_ready is not None:
            value["nativeReady"] = native_ready
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return RuntimeTransportReply(
            request_id=request_id,
            schema_id="relay.liveloop.command-result",
            schema_version=1,
            media_type="application/json",
            payload=payload,
            runtime_changed=changed,
            runtime_revision_after=self.runtime_revision,
        )


class NativeRuntimeWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-runtime-wiring-")
        self.root = Path(self.temporary.name)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])
        self.transport = FakeAuthenticatedPlayerTransport()
        session = RuntimeSessionConfig(
            SESSION_ID,
            LAUNCH_ID,
            self.transport.runtime_revision,
            1,
            b"synthetic-only-secret-material-32-bytes",
            "127.0.0.1",
            0,
            RESOURCE_RELEASE,
        )
        self.preparation, self.runtime = create_native_update_providers(self.transport, self.artifacts, session)
        self.service = build_command_service(
            self.ledger,
            self.artifacts,
            preparation_provider=self.preparation,
            runtime_provider=self.runtime,
        )
        self.request_counter = 0

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _request_id(self, prefix: str) -> str:
        self.request_counter += 1
        return f"{prefix}-{self.request_counter}"

    def _command(self, operation: str, task_id: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = {
            "protocolVersion": 1,
            "requestId": self._request_id(operation.replace(".", "-")),
            "operation": operation,
            "arguments": dict(arguments),
        }
        if task_id is not None:
            envelope["taskId"] = task_id
            envelope["arguments"].setdefault("taskId", task_id)
        return self.service.execute(envelope)

    def _open_task(self, route: str, *, session_id: str = SESSION_ID) -> str:
        impact = _impact(route)
        response = self._command(
            "task.open",
            None,
            {
                "goal": "Exercise a synthetic Native update.",
                "sessionId": session_id,
                "target": "Synthetic target",
                "allowedImpact": impact,
                "acceptance": ["Synthetic Player state matches the plan."],
            },
        )
        self.assertEqual("completed", response["status"])
        return response["result"]["taskId"]

    def _bind_manifest(
        self,
        task_id: str,
        route: str,
        *,
        authorized_module_closure: list[str] | None = None,
    ) -> Path:
        payload_kind = "runtime_hotfix_assembly" if route == "HOTFIX" else "runtime_reload_dll"
        payload_path = self.artifact_root / f"{route.lower()}-payload.bin"
        payload_path.write_bytes(b"synthetic-native-payload-v1")
        payload = self.artifacts.register(payload_path, kind=payload_kind, task_id=task_id)
        generations = {
            "moduleGeneration": self.transport.module_generation,
            "resourceRelease": RESOURCE_RELEASE,
            "viewGeneration": self.transport.view_generation,
        }
        common: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "version": 1,
            "taskId": task_id,
            "sessionId": SESSION_ID,
            "launchId": LAUNCH_ID,
            "inputSnapshot": "snapshot-synthetic-1",
            "route": route,
            "moduleId": MODULE_ID,
            "dependencyClosure": [MODULE_ID],
            "expectedRuntimeRevision": self.transport.runtime_revision,
            "targetGenerations": generations,
            "requiredImpact": _impact(route),
            "affectedModules": [MODULE_ID],
            "affectedViews": [],
        }
        if route == "HOTFIX":
            common.update(
                {
                    "expectedRuntimeRevisionAfter": runtime_revision_after(self.transport.runtime_revision, task_id, "hotfix"),
                    "targetGenerationsAfter": dict(generations),
                    "assemblyArtifactId": payload["artifactId"],
                    "assemblyName": "Synthetic.Assembly",
                    "expectedAssemblyGeneration": self.transport.module_generation,
                    "types": [{"typeName": "Synthetic.Type", "signatures": ["System.Void Synthetic.Type::Apply()"]}],
                }
            )
        else:
            next_generation = self.transport.module_generation + 1
            common.update(
                {
                    "expectedRuntimeRevisionAfter": None,
                    "targetGenerationsAfter": {
                        "moduleGeneration": next_generation,
                        "resourceRelease": RESOURCE_RELEASE,
                        "viewGeneration": self.transport.view_generation + 1,
                    },
                    "resourceRelease": RESOURCE_RELEASE,
                    "disposedGeneration": self.transport.module_generation,
                    "nextGeneration": next_generation,
                    "entryAssemblyName": "Synthetic.Assembly",
                    "authorizedModuleClosure": authorized_module_closure or [MODULE_ID],
                    "assemblies": [{"name": "Synthetic.Assembly", "dependencies": []}],
                    "payloads": [{
                        "name": "Synthetic.Assembly",
                        "expectedGeneration": self.transport.module_generation,
                        "generationAfter": next_generation,
                        "dllArtifactId": payload["artifactId"],
                        "pdbArtifactId": None,
                    }],
                }
            )
        manifest_path = self.artifact_root / f"{route.lower()}-manifest.json"
        manifest_path.write_text(json.dumps(common, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        manifest = self.artifacts.register(manifest_path, kind=MANIFEST_KIND, task_id=task_id, media_type="application/json")
        descriptor = {
            "schema": PREPARATION_SCHEMA,
            "version": 1,
            "inputSnapshot": "snapshot-synthetic-1",
            "manifestArtifactId": manifest["artifactId"],
            "artifactIds": [manifest["artifactId"], payload["artifactId"]],
        }
        update = self._command("task.update", task_id, {"updates": {"reference": json.dumps(descriptor, separators=(",", ":"))}})
        self.assertEqual("completed", update["status"])
        return payload_path

    def _prepared_plan(self, route: str = "MODULE_RELOAD", *, session_id: str = SESSION_ID) -> tuple[str, Path, dict[str, Any]]:
        task_id = self._open_task(route, session_id=session_id)
        payload_path = self._bind_manifest(task_id, route)
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"])
        job = self._wait_job(accepted["jobId"])
        self.assertEqual("completed", job["state"], job)
        return task_id, payload_path, job["result"]["plan"]

    def _wait_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.ledger.get_job(job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail(f"job did not finish: {job_id}")

    def _iterate(self, task_id: str, plan: dict[str, Any], request_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        args = {"taskId": task_id, "planId": plan["planId"]}
        envelope = {
            "protocolVersion": 1,
            "requestId": request_id or self._request_id("iterate"),
            "operation": "iterate",
            "taskId": task_id,
            "arguments": args,
        }
        accepted = self.service.execute(envelope)
        self.assertEqual("accepted", accepted["status"], accepted)
        return accepted, self._wait_job(accepted["jobId"])

    def test_production_composition_module_reload_success(self) -> None:
        self.assertIs(self.service.coordinator.preparation_provider, self.preparation)
        self.assertIs(self.service.coordinator.runtime_provider, self.runtime)
        self.assertNotIn("hotfix", OPERATIONS)
        self.assertFalse(any(name.startswith("module.") for name in OPERATIONS))
        task_id, _payload, plan = self._prepared_plan()
        self.assertIsNone(plan["details"]["expectedRuntimeRevisionAfter"])
        self.assertEqual(
            {"moduleGeneration": 3, "resourceRelease": RESOURCE_RELEASE, "viewGeneration": 5},
            plan["details"]["targetGenerationsAfter"],
        )
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("completed", job["state"], job)
        self.assertEqual(["module.quiesce", "module.dispose", "module.load", "module.restore"], self.transport.mutations)
        self.assertEqual(3, self.transport.module_generation)
        self.assertEqual(5, self.transport.view_generation)
        self.assertTrue(self.ledger.get_task(task_id)["facts"]["runtimeMatched"])
        self.assertNotIn("runtime.update.observe", [call["operation"] for call in self.transport.calls])
        self.assertTrue(all(call["command"]["taskId"] == task_id for call in self.transport.calls))

    def test_production_composition_hotfix_success_and_digest(self) -> None:
        task_id, _payload, plan = self._prepared_plan("HOTFIX")
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("completed", job["state"], job)
        self.assertEqual(["hotfix"], self.transport.mutations)
        command = next(call["command"] for call in self.transport.calls if call["operation"] == "hotfix")
        self.assertEqual(canonical_input_sha256(command), command["arguments"]["inputSha256"])
        self.assertEqual(plan["details"]["expectedRuntimeRevisionAfter"], job["result"]["runtimeRevisionAfter"])

    def test_missing_observation_identity_fails_before_any_mutation(self) -> None:
        self.transport.missing_module_id = True
        task_id = self._open_task("MODULE_RELOAD")
        self._bind_manifest(task_id, "MODULE_RELOAD")
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"])
        job = self._wait_job(accepted["jobId"])
        self.assertEqual("failed", job["state"])
        self.assertEqual("WRONG_SESSION", job["error"]["code"])
        self.assertEqual([], self.transport.mutations)

    def test_wrong_task_session_is_rejected_by_shared_prepare_path(self) -> None:
        task_id = self._open_task("MODULE_RELOAD", session_id="session-other")
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"])
        job = self._wait_job(accepted["jobId"])
        self.assertEqual("failed", job["state"])
        self.assertEqual("WRONG_SESSION", job["error"]["code"])
        self.assertEqual([], self.transport.calls)

    def test_artifact_bytes_mismatch_fails_before_runtime_mutation(self) -> None:
        task_id, payload_path, plan = self._prepared_plan()
        payload_path.write_bytes(b"changed-after-immutable-plan")
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("failed", job["state"])
        self.assertEqual("INPUT_CHANGED", job["error"]["code"])
        self.assertEqual([], self.transport.mutations)

    def test_artifact_change_after_coordinator_check_fails_before_quiesce(self) -> None:
        task_id, payload_path, plan = self._prepared_plan()
        self.transport.mutate_payload_on_capture = payload_path
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("failed", job["state"], job)
        self.assertEqual([], self.transport.mutations)

    def test_stale_generation_is_rejected_and_duplicate_request_is_idempotent(self) -> None:
        task_id, _payload, plan = self._prepared_plan()
        self.transport.module_generation += 1
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("failed", job["state"])
        self.assertEqual("STALE_TARGET", job["error"]["code"])
        self.assertEqual([], self.transport.mutations)

    def test_duplicate_iterate_request_does_not_repeat_mutations(self) -> None:
        task_id, _payload, plan = self._prepared_plan()
        request_id = "iterate-duplicate-stable"
        accepted, job = self._iterate(task_id, plan, request_id=request_id)
        self.assertEqual("completed", job["state"])
        mutation_count = len(self.transport.mutations)
        replay = self.service.execute({
            "protocolVersion": 1,
            "requestId": request_id,
            "operation": "iterate",
            "taskId": task_id,
            "arguments": {"taskId": task_id, "planId": plan["planId"]},
        })
        self.assertEqual(accepted["jobId"], replay["jobId"])
        self.assertEqual(mutation_count, len(self.transport.mutations))

    def test_partial_execution_lost_reply_is_state_unknown_without_replay(self) -> None:
        task_id, _payload, plan = self._prepared_plan()
        self.transport.lose_on_operation = "module.load"
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("state_unknown", job["state"], job)
        self.assertEqual(["module.quiesce", "module.dispose", "module.load"], self.transport.mutations)
        self.assertFalse(self.transport.authenticated)
        self.assertTrue(job["error"]["details"].get("freshSessionRequired"))
        self.assertEqual(1, self.transport.mutations.count("module.load"))
        self.assertEqual(1, [call["operation"] for call in self.transport.calls].count("module.load"))

    def test_unknown_error_attribution_discards_session_before_reconciliation(self) -> None:
        task_id, _payload, plan = self._prepared_plan()

        class SyntheticSocket:
            def settimeout(self, _timeout: float) -> None:
                pass

            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        revision = self.transport.runtime_revision
        loopback = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision=revision,
        )
        loopback._active_socket = SyntheticSocket()
        loopback._connection_id = "connection-synthetic"
        loopback._connection_key = bytearray(b"synthetic-connection-key-material")
        sent: list[dict[str, Any]] = []

        def receive(_active: Any) -> dict[str, Any]:
            request = sent[-1]
            operation = request["operation"]
            request_id = request["requestId"]
            if operation == "module.quiesce":
                return {
                    "kind": "error",
                    "requestId": request_id,
                    "error": {
                        "code": "STATE_UNKNOWN",
                        "stage": "runtime_apply",
                        "runtimeChangedKnown": False,
                        "runtimeChanged": None,
                        "recoverable": False,
                        "details": [],
                    },
                }
            if operation == "module.observe":
                result = {
                    "moduleId": MODULE_ID,
                    "moduleGeneration": self.transport.module_generation,
                    "stage": "active",
                    "state": {"resourceRelease": RESOURCE_RELEASE},
                }
            elif operation == "module.capture_context":
                result = {
                    "schemaId": "synthetic.context",
                    "schemaVersion": 1,
                    "mediaType": "application/json",
                    "sourceSessionId": SESSION_ID,
                    "sourceModuleGeneration": self.transport.module_generation,
                    "sourceViewGeneration": self.transport.view_generation,
                    "dataRevision": "context-synthetic-1",
                    "payloadBase64": base64.b64encode(b"{}").decode("ascii"),
                }
            else:
                raise AssertionError(f"unexpected follow-up operation on the retired session: {operation}")
            payload = json.dumps(
                {
                    "status": "completed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": revision,
                    "result": result,
                    "error": None,
                    **({"nativeReady": True} if operation == "module.observe" else {}),
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return {
                "kind": "response",
                "requestId": request_id,
                "runtimeChangedKnown": True,
                "runtimeChanged": False,
                "runtimeRevision": revision,
                "schemaId": "relay.liveloop.command-result",
                "schemaVersion": 1,
                "mediaType": "application/json",
                "payloadBase64": base64.b64encode(payload).decode("ascii"),
            }

        loopback._send_message = lambda _active, message: sent.append(message)
        loopback._receive_message = receive
        self.runtime._transport = loopback

        _accepted, job = self._iterate(task_id, plan)
        operations = [message["operation"] for message in sent]

        self.assertEqual("state_unknown", job["state"], job)
        self.assertFalse(loopback.authenticated)
        self.assertEqual("module.quiesce", operations[-1])
        self.assertNotIn("module.reconcile", operations)
        self.assertTrue(job["error"]["details"].get("freshSessionRequired"))

    def test_inner_failed_null_attribution_cannot_be_downgraded_by_known_false_outer_reply(self) -> None:
        task_id, _payload, plan = self._prepared_plan()

        class SyntheticSocket:
            def settimeout(self, _timeout: float) -> None:
                pass

            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        revision = self.transport.runtime_revision
        loopback = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision=revision,
        )
        loopback._active_socket = SyntheticSocket()
        loopback._connection_id = "connection-synthetic"
        loopback._connection_key = bytearray(b"synthetic-connection-key-material")
        sent: list[dict[str, Any]] = []

        def receive(_active: Any) -> dict[str, Any]:
            request = sent[-1]
            operation = request["operation"]
            request_id = request["requestId"]
            if operation == "module.quiesce":
                result_payload = {
                    "status": "failed",
                    "runtimeChanged": None,
                    "runtimeRevisionAfter": revision,
                    "result": {},
                    "error": {"code": "STATE_UNKNOWN", "stage": "runtime_apply"},
                }
            elif operation == "module.observe":
                result_payload = {
                    "status": "completed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": revision,
                    "result": {
                        "moduleId": MODULE_ID,
                        "moduleGeneration": self.transport.module_generation,
                        "stage": "active",
                        "state": {"resourceRelease": RESOURCE_RELEASE},
                    },
                    "nativeReady": True,
                    "error": None,
                }
            elif operation == "module.capture_context":
                result_payload = {
                    "status": "completed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": revision,
                    "result": {
                        "schemaId": "synthetic.context",
                        "schemaVersion": 1,
                        "mediaType": "application/json",
                        "sourceSessionId": SESSION_ID,
                        "sourceModuleGeneration": self.transport.module_generation,
                        "sourceViewGeneration": self.transport.view_generation,
                        "dataRevision": "context-synthetic-1",
                        "payloadBase64": base64.b64encode(b"{}").decode("ascii"),
                    },
                    "error": None,
                }
            else:
                raise AssertionError(f"unexpected operation after an ambiguous inner result: {operation}")
            return {
                "kind": "response",
                "requestId": request_id,
                "runtimeChangedKnown": True,
                "runtimeChanged": False,
                "runtimeRevision": revision,
                "schemaId": "relay.liveloop.command-result",
                "schemaVersion": 1,
                "mediaType": "application/json",
                "payloadBase64": base64.b64encode(
                    json.dumps(result_payload, separators=(",", ":")).encode("utf-8")
                ).decode("ascii"),
            }

        loopback._send_message = lambda _active, message: sent.append(message)
        loopback._receive_message = receive
        self.runtime._transport = loopback

        _accepted, job = self._iterate(task_id, plan)
        operations = [message["operation"] for message in sent]

        self.assertEqual("state_unknown", job["state"], job)
        self.assertFalse(loopback.authenticated)
        self.assertEqual("module.quiesce", operations[-1])
        self.assertNotIn("module.reconcile", operations)
        self.assertTrue(job["error"]["details"].get("freshSessionRequired"))

    def test_actual_csharp_unknown_error_wire_retires_host_session_before_reconcile(self) -> None:
        wire_path = os.environ.get("RELAY_LIVELOOP_CSHARP_UNKNOWN_WIRE")
        if not wire_path:
            self.skipTest("run tests/unity-synthetic/native-unknown/run-synthetic.ps1 to generate the actual C# adapter wire fixture")
        wire_message = json.loads(Path(wire_path).read_text(encoding="utf-8"))
        expected_request_id = "runtime_" + ("a" * 32)
        self.assertEqual("error", wire_message.get("kind"), wire_message)
        self.assertEqual(expected_request_id, wire_message.get("requestId"), wire_message)
        self.assertFalse(wire_message.get("runtimeChangedKnown"), wire_message)
        self.assertEqual("STATE_UNKNOWN", wire_message.get("error", {}).get("code"), wire_message)
        self.assertFalse(wire_message.get("error", {}).get("runtimeChangedKnown"), wire_message)

        task_id, _payload, plan = self._prepared_plan()

        class SyntheticSocket:
            def settimeout(self, _timeout: float) -> None:
                pass

            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        revision = self.runtime._runtime_revision
        loopback = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision=revision,
        )
        loopback._active_socket = SyntheticSocket()
        loopback._connection_id = "connection-synthetic"
        loopback._connection_key = bytearray(b"synthetic-connection-key-material")
        sent: list[dict[str, Any]] = []

        def receive(_active: Any) -> dict[str, Any]:
            request = sent[-1]
            operation = request["operation"]
            request_id = request["requestId"]
            if operation == "module.quiesce":
                self.assertEqual(expected_request_id, request_id)
                return wire_message
            if operation == "module.observe":
                result_payload = {
                    "status": "completed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": revision,
                    "result": {
                        "moduleId": MODULE_ID,
                        "moduleGeneration": self.transport.module_generation,
                        "stage": "active",
                        "state": {"resourceRelease": RESOURCE_RELEASE},
                    },
                    "nativeReady": True,
                    "error": None,
                }
            elif operation == "module.capture_context":
                result_payload = {
                    "status": "completed",
                    "runtimeChanged": False,
                    "runtimeRevisionAfter": revision,
                    "result": {
                        "schemaId": "synthetic.context",
                        "schemaVersion": 1,
                        "mediaType": "application/json",
                        "sourceSessionId": SESSION_ID,
                        "sourceModuleGeneration": self.transport.module_generation,
                        "sourceViewGeneration": self.transport.view_generation,
                        "dataRevision": "context-synthetic-1",
                        "payloadBase64": base64.b64encode(b"{}").decode("ascii"),
                    },
                    "error": None,
                }
            else:
                raise AssertionError(f"unexpected operation after actual C# unknown terminal: {operation}")
            return {
                "kind": "response",
                "requestId": request_id,
                "runtimeChangedKnown": True,
                "runtimeChanged": False,
                "runtimeRevision": revision,
                "schemaId": "relay.liveloop.command-result",
                "schemaVersion": 1,
                "mediaType": "application/json",
                "payloadBase64": base64.b64encode(
                    json.dumps(result_payload, separators=(",", ":")).encode("utf-8")
                ).decode("ascii"),
            }

        loopback._send_message = lambda _active, message: sent.append(message)
        loopback._receive_message = receive
        self.runtime._transport = loopback

        call_count = 0

        def deterministic_request_id() -> UUID:
            nonlocal call_count
            call_count += 1
            if call_count == 7:
                return UUID(hex="a" * 32)
            return uuid4()

        with patch("host.runtime_update_provider.uuid4", side_effect=deterministic_request_id):
            _accepted, job = self._iterate(task_id, plan)
        operations = [message["operation"] for message in sent]

        self.assertEqual("state_unknown", job["state"], {
            "job": job,
            "sentOperations": operations,
            "sentRequestIds": [message["requestId"] for message in sent],
            "requestIdCalls": call_count,
        })
        self.assertFalse(loopback.authenticated)
        self.assertEqual("module.quiesce", operations[-1])
        self.assertEqual(5, len(operations), operations)
        self.assertEqual(7, call_count)
        self.assertNotIn("module.reconcile", operations)
        self.assertTrue(job["error"]["details"].get("freshSessionRequired"))

    def test_module_reconcile_is_diagnostic_and_cannot_clear_unknown_state(self) -> None:
        task_id, _payload, plan = self._prepared_plan()
        self.runtime._mutation_may_have_started = True
        result = self.runtime.reconcile(plan, {"jobId": "job-synthetic"})
        self.assertEqual("state_unknown", result["status"])
        self.assertFalse(result["error"]["recoverable"])
        self.assertEqual("module.reconcile", self.transport.calls[-1]["operation"])
        self.assertEqual({"moduleId": MODULE_ID}, self.transport.calls[-1]["command"]["arguments"])
        self.assertEqual([], self.transport.mutations)

    def test_native_ready_false_sends_no_destructive_module_operation(self) -> None:
        task_id, _payload, plan = self._prepared_plan()
        self.transport.native_ready = False
        _accepted, job = self._iterate(task_id, plan)
        self.assertEqual("failed", job["state"], job)
        self.assertEqual([], self.transport.mutations)
        called = [call["operation"] for call in self.transport.calls]
        self.assertIn("module.observe", called)
        self.assertNotIn("module.quiesce", called)
        self.assertNotIn("module.dispose", called)

    def test_mismatched_authorized_module_closure_fails_before_any_mutation(self) -> None:
        task_id = self._open_task("MODULE_RELOAD")
        self._bind_manifest(task_id, "MODULE_RELOAD", authorized_module_closure=[MODULE_ID, "Other.Module"])

        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"])
        job = self._wait_job(accepted["jobId"])

        self.assertEqual("failed", job["state"], job)
        self.assertEqual("CONTRACT_MISMATCH", job["error"]["code"])
        self.assertEqual([], self.transport.mutations)


class RuntimeTransportUnknownAttributionTests(unittest.TestCase):
    class _Socket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def shutdown(self, _how: int) -> None:
            pass

        def close(self) -> None:
            pass

    def test_nullable_runtime_attribution_is_returned_and_session_discarded(self) -> None:
        transport = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision="revision-synthetic-0",
        )
        active = self._Socket()
        transport._active_socket = active
        transport._connection_id = "connection-synthetic"
        transport._connection_key = bytearray(b"synthetic-connection-key-material")
        request_id = "request-synthetic-unknown"
        payload = json.dumps(
            {
                "status": "state_unknown",
                "runtimeChanged": None,
                "runtimeRevisionAfter": None,
                "result": {},
                "error": {"code": "STATE_UNKNOWN"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = {
            "kind": "response",
            "requestId": request_id,
            "runtimeChangedKnown": False,
            "runtimeChanged": None,
            "runtimeRevision": None,
            "schemaId": "relay.liveloop.command-result",
            "schemaVersion": 1,
            "mediaType": "application/json",
            "payloadBase64": base64.b64encode(payload).decode("ascii"),
        }
        transport._send_message = lambda _active, _message: None
        transport._receive_message = lambda _active: response

        reply = transport.invoke(request_id=request_id, operation="module.load", payload=b"synthetic request")

        self.assertIsNone(reply.runtime_changed)
        self.assertIsNone(reply.runtime_revision_after)
        self.assertFalse(transport.authenticated)

    def test_unknown_error_envelope_discards_session_and_prevents_a_second_send(self) -> None:
        transport = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision="revision-synthetic-0",
        )
        active = self._Socket()
        transport._active_socket = active
        transport._connection_id = "connection-synthetic"
        transport._connection_key = bytearray(b"synthetic-connection-key-material")
        request_id = "request-synthetic-error-unknown"
        response = {
            "kind": "error",
            "requestId": request_id,
            "error": {
                "code": "STATE_UNKNOWN",
                "stage": "runtime_apply",
                "runtimeChangedKnown": False,
                "runtimeChanged": None,
                "recoverable": False,
                "details": [],
            },
        }
        sent: list[dict[str, Any]] = []
        transport._send_message = lambda _active, message: sent.append(message)
        transport._receive_message = lambda _active: response

        with self.assertRaises(CommandError) as raised:
            transport.invoke(request_id=request_id, operation="module.load", payload=b"synthetic request")

        self.assertIsNone(raised.exception.runtime_changed)
        self.assertFalse(transport.authenticated)
        self.assertEqual(1, len(sent))
        with self.assertRaises(CommandError) as second:
            transport.invoke(request_id="request-synthetic-second", operation="module.observe", payload=b"synthetic follow-up")
        self.assertEqual("CAPABILITY_UNAVAILABLE", second.exception.code)
        self.assertEqual(1, len(sent), "the discarded session must not dispatch follow-up work")

    def test_known_no_change_error_keeps_session_and_attribution_semantics(self) -> None:
        transport = LoopbackRuntimeHostTransport(
            shared_secret=b"synthetic-only-secret-material-32-bytes",
            expected_session_id=SESSION_ID,
            expected_launch_id=LAUNCH_ID,
            expected_runtime_revision="revision-synthetic-0",
        )
        active = self._Socket()
        transport._active_socket = active
        transport._connection_id = "connection-synthetic"
        transport._connection_key = bytearray(b"synthetic-connection-key-material")
        response = {
            "kind": "error",
            "requestId": "request-synthetic-error-known-no-change",
            "error": {
                "code": "STALE_TARGET",
                "stage": "runtime_apply",
                "runtimeChangedKnown": True,
                "runtimeChanged": False,
                "recoverable": False,
                "details": [],
            },
        }
        transport._send_message = lambda _active, _message: None
        transport._receive_message = lambda _active: response

        with self.assertRaises(CommandError) as raised:
            transport.invoke(
                request_id="request-synthetic-error-known-no-change",
                operation="module.load",
                payload=b"synthetic request",
            )

        self.assertIs(raised.exception.runtime_changed, False)
        self.assertTrue(transport.authenticated)


class NativeRuntimeServeCompositionTests(unittest.TestCase):
    class _Transport(FakeAuthenticatedPlayerTransport):
        def __init__(self) -> None:
            super().__init__()
            self.started = False
            self.closed = False

        def start(self) -> None:
            self.started = True

        def close(self) -> None:
            self.closed = True

    class _Server:
        def __init__(self, service: Any) -> None:
            self.service = service
            self.closed = False

        def serve_forever(self, poll_interval: float) -> None:
            del poll_interval
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    def test_serve_loads_runtime_config_wires_native_providers_and_closes_resources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-runtime-serve-") as temporary:
            root = Path(temporary)
            session_path = root / "runtime-session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "sessionId": SESSION_ID,
                        "launchId": LAUNCH_ID,
                        "runtimeRevision": "revision-synthetic-0",
                        "sharedSecretBase64": base64.b64encode(b"synthetic-only-secret-material-32-bytes").decode("ascii"),
                        "hostAddress": "127.0.0.1",
                        "port": 18761,
                        "resourceReleaseId": RESOURCE_RELEASE,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            project_root = root / "synthetic-project"
            (project_root / "Assets").mkdir(parents=True)
            (project_root / "Baselines").mkdir()
            unity_root = root / "synthetic-unity"
            unity_root.mkdir()
            (project_root / "Assets" / "Synthetic.cs").write_text("public sealed class SyntheticType {}", encoding="utf-8")
            baseline_path = project_root / "Baselines" / "Synthetic.Assembly.dll"
            baseline_bytes = b"synthetic immutable baseline assembly"
            baseline_path.write_bytes(baseline_bytes)
            profile_path = root / "native-compile-profiles.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema": "relay.liveloop.native-compile-profiles",
                        "version": 2,
                        "profiles": [
                            {
                                "profileId": "synthetic-profile",
                                "target": "Synthetic target",
                                "projectRoot": str(project_root),
                                "unityRoot": str(unity_root),
                                "unityVersion": "2022.3.62f3",
                                "buildTarget": "StandaloneWindows64",
                                "buildTargetGroup": "Standalone",
                                "configuration": "Debug",
                                "developmentBuild": True,
                                "subtarget": 0,
                                "extraScriptingDefines": [],
                                "defines": [],
                                "references": [],
                                "sourceInputs": ["Assets/Synthetic.cs"],
                                "moduleId": MODULE_ID,
                                "dependencyClosure": [MODULE_ID],
                                "initialModuleGeneration": 2,
                                "entryAssemblyName": "Synthetic.Assembly",
                                "assemblies": [
                                    {
                                        "name": "Synthetic.Assembly",
                                        "moduleId": MODULE_ID,
                                        "dependencies": [],
                                        "baselinePath": "Baselines/Synthetic.Assembly.dll",
                                        "baselineSha256": hashlib.sha256(baseline_bytes).hexdigest(),
                                    }
                                ],
                                "timeoutSeconds": 3,
                            }
                        ],
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            transport = self._Transport()
            servers: list[NativeRuntimeServeCompositionTests._Server] = []
            transport_kwargs: list[dict[str, Any]] = []

            def make_transport(**kwargs: Any) -> NativeRuntimeServeCompositionTests._Transport:
                transport_kwargs.append(kwargs)
                return transport

            def make_server(service: Any, *_args: Any) -> NativeRuntimeServeCompositionTests._Server:
                server = self._Server(service)
                servers.append(server)
                return server

            args = argparse.Namespace(
                database=str(root / "host.sqlite3"),
                artifact_root=[str(root / "artifacts")],
                machine_config=None,
                runtime_session_file=str(session_path),
                native_compile_profiles=str(profile_path),
                editor_job_root=str(root / "editor-jobs"),
                editor_artifact_root=str(root / "artifacts" / "editor"),
                host="127.0.0.1",
                port=0,
            )
            with (
                patch.object(relay_liveloop, "_token_from_args", return_value="synthetic-token"),
                patch.object(relay_liveloop, "LoopbackRuntimeHostTransport", side_effect=make_transport),
                patch.object(relay_liveloop, "create_http_server", side_effect=make_server),
            ):
                result = relay_liveloop._serve(args)

            self.assertEqual(0, result)
            self.assertTrue(transport.started)
            self.assertTrue(transport.closed)
            self.assertTrue(servers[0].closed)
            self.assertEqual(16 * 1024 * 1024, transport_kwargs[0]["maximum_frame_bytes"])
            service = servers[0].service
            self.assertEqual("NativeSourcePreparationProvider", type(service.coordinator.preparation_provider).__name__)
            self.assertEqual("PlayerRuntimeUpdateProvider", type(service.coordinator.runtime_provider).__name__)
            self.assertTrue(service.coordinator.runtime_provider.is_verified)
            self.assertEqual(root / "editor-jobs", service.coordinator.preparation_provider._editor.job_root)

    def test_player_bound_serve_rejects_manual_manifest_only_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-runtime-serve-missing-compile-") as temporary:
            root = Path(temporary)
            session_path = root / "runtime-session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "sessionId": SESSION_ID,
                        "launchId": LAUNCH_ID,
                        "runtimeRevision": "revision-synthetic-0",
                        "sharedSecretBase64": base64.b64encode(b"synthetic-only-secret-material-32-bytes").decode("ascii"),
                        "hostAddress": "127.0.0.1",
                        "port": 18761,
                        "resourceReleaseId": RESOURCE_RELEASE,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                database=str(root / "host.sqlite3"),
                artifact_root=[str(root / "artifacts")],
                machine_config=None,
                runtime_session_file=str(session_path),
                host="127.0.0.1",
                port=0,
            )
            with patch.object(relay_liveloop, "_token_from_args", return_value="synthetic-token"):
                with self.assertRaisesRegex(ValueError, "manual manifest preparation is disabled"):
                    relay_liveloop._serve(args)


if __name__ == "__main__":
    unittest.main()
