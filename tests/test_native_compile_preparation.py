from __future__ import annotations

import hashlib
import json
import tempfile
import time
import uuid
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from host.artifacts import ArtifactStore
from host.composite_update import CompositePreparationProvider, artifact_closure_sha256
from host.editor_transport import EditorJobEnvelope, EditorJobTransport, EditorJobTicket
from host.errors import CommandError
from host.ledger import Ledger
from host.native_compile_profile import (
    NativeCompileProfileRegistry,
    native_compile_profile_digest,
    native_input_snapshot,
)
from host.native_compile_receipt import (
    COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY,
    REQUIRED_COMPILER_INPUT_LIMITATIONS,
    TOOLCHAIN_REQUIRED_RELATIVE,
    expected_project_configuration_inputs,
    expected_unity_toolchain_inputs,
    native_compile_input_set_digest,
)
from host.runtime_session import RuntimeSessionConfig
from host.runtime_update_provider import create_native_update_providers
from relay_liveloop import build_command_service
from tests.test_native_runtime_wiring import (
    FakeAuthenticatedPlayerTransport,
    LAUNCH_ID,
    MODULE_ID,
    RESOURCE_RELEASE,
    SESSION_ID,
    _impact,
)


class SyntheticEditorJobTransport(EditorJobTransport):
    """Real Host file transport with a deterministic synthetic Editor worker at its I/O edge."""

    def __init__(
        self,
        job_root: Path,
        artifact_root: Path,
        project_root: Path,
        unity_root: Path,
        *,
        mode: str = "hotfix",
        ledger: Ledger | None = None,
    ) -> None:
        super().__init__(job_root, artifact_root)
        self.project_root = project_root
        self.unity_root = unity_root
        self.mode = mode
        self.ledger = ledger
        self.enqueue_submitted: list[bool] = []
        self.enqueue_bindings: list[dict[str, Any] | None] = []
        self.wait_calls = 0
        self.compile_count = 0
        self.interrupt_after_result = False
        self.timeout_before_result = False
        self.source_to_mutate: Path | None = None
        self.baseline_to_mutate: Path | None = None
        self.task_to_mutate: str | None = None
        self.receipt_limitations: list[str] | None = None

    def enqueue(self, envelope: EditorJobEnvelope, *, allow_submit: bool = True) -> EditorJobTicket:
        ticket = super().enqueue(envelope, allow_submit=allow_submit)
        self.enqueue_submitted.append(ticket.submitted)
        if self.ledger is None:
            self.enqueue_bindings.append(None)
        else:
            job = self.ledger.get_job(envelope.job_id)
            result = job.get("result") or {}
            self.enqueue_bindings.append(result)
        return ticket

    def wait(
        self,
        ticket: EditorJobTicket,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, Any]:
        self.wait_calls += 1
        if self.timeout_before_result:
            self.timeout_before_result = False
            raise CommandError(
                "TIMEOUT",
                "Synthetic Editor worker has not written a durable result.",
                stage="editor_wait",
                runtime_changed=False,
                recoverable=True,
            )
        self._complete(ticket)
        if self.interrupt_after_result:
            self.interrupt_after_result = False
            raise CommandError(
                "TIMEOUT",
                "Synthetic interruption after the durable Editor result was sealed.",
                stage="editor_wait",
                runtime_changed=False,
                recoverable=True,
            )
        return super().wait(ticket, timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds)

    def _complete(self, ticket: EditorJobTicket) -> None:
        result_path = self.job_root / "results" / f"{ticket.job_id}.result.json"
        if result_path.exists():
            return
        incoming = self.job_root / "incoming" / f"{ticket.job_id}.request.json"
        processing = self.job_root / "processing" / f"{ticket.job_id}.request.json"
        request_path = incoming if incoming.is_file() else processing
        if not request_path.is_file():
            raise AssertionError("synthetic Editor worker could not find the durable Host request")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        payload = json.loads(request["payloadJson"])
        context = json.loads(payload["providerContextJson"])
        if incoming.is_file():
            incoming.replace(processing)
        self.compile_count += 1

        artifacts: list[dict[str, Any]] = []
        artifact_directory = self.artifact_root / ticket.job_id
        artifact_directory.mkdir(parents=True, exist_ok=True)

        def add_artifact(suffix: str, kind: str, media_type: str, contents: bytes) -> str:
            artifact_id = f"editor-{ticket.job_id}-{suffix}"
            path = artifact_directory / f"{artifact_id}{Path(suffix).suffix or '.bin'}"
            path.write_bytes(contents)
            item = {
                "artifactId": artifact_id,
                "kind": kind,
                "path": str(path),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "mediaType": media_type,
                "size": len(contents),
            }
            artifacts.append(item)
            return artifact_id

        compile_manifest_id = add_artifact(
            "compile-manifest.json",
            "compile-manifest",
            "application/json",
            json.dumps(
                {"jobId": ticket.job_id, "inputSnapshot": ticket.input_snapshot, "synthetic": True},
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        file_records: list[dict[str, Any]] = []
        for assembly in context["assemblies"]:
            dll_bytes = f"compiled:{self.mode}:{assembly['name']}".encode("utf-8")
            dll_id = add_artifact(
                f"{assembly['name']}.dll",
                "managed-assembly",
                "application/octet-stream",
                dll_bytes,
            )
            file_records.append({
                "artifactId": dll_id,
                "assemblyName": assembly["name"],
                "extension": ".dll",
                "sha256": hashlib.sha256(dll_bytes).hexdigest(),
                "size": len(dll_bytes),
            })
            pdb_bytes = f"symbols:{self.mode}:{assembly['name']}".encode("utf-8")
            pdb_id = add_artifact(
                f"{assembly['name']}.pdb",
                "managed-symbols",
                "application/octet-stream",
                pdb_bytes,
            )
            file_records.append({
                "artifactId": pdb_id,
                "assemblyName": assembly["name"],
                "extension": ".pdb",
                "sha256": hashlib.sha256(pdb_bytes).hexdigest(),
                "size": len(pdb_bytes),
            })
        compile_attempt = "compile-" + uuid.uuid4().hex
        input_roles: dict[tuple[str, str], set[str]] = {}

        def add_input(scope: str, relative: str, role: str) -> None:
            key = (scope, relative.replace("\\", "/"))
            input_roles.setdefault(key, set()).add(role)

        synthetic_profile = SimpleNamespace(project_root=self.project_root, unity_root=self.unity_root)
        for relative, roles in expected_project_configuration_inputs(synthetic_profile).items():
            for role in roles:
                add_input("project", relative, role)
        for relative, roles in expected_unity_toolchain_inputs(synthetic_profile).items():
            for role in roles:
                add_input("unity", relative, role)
        source_path = self.project_root / "Assets" / "Synthetic.cs"
        reference_path = self.project_root / "Assets" / "ExtraReference.dll"
        assembly_name = context["assemblies"][0]["name"]
        add_input("project", source_path.relative_to(self.project_root).as_posix(), f"assembly-source:{assembly_name}")
        add_input("project", reference_path.relative_to(self.project_root).as_posix(), f"assembly-reference:{assembly_name}")
        receipt_inputs = []
        for (scope, relative), roles in sorted(input_roles.items()):
            root = self.project_root if scope == "project" else self.unity_root
            path = root / Path(relative)
            contents = path.read_bytes()
            receipt_inputs.append({
                "scope": scope,
                "path": relative,
                "roles": sorted(roles),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size": len(contents),
            })
        compiler_options = {
            "additionalCompilerArguments": [],
            "allowUnsafeCode": False,
            "analyzerConfigPath": "",
            "apiCompatibilityLevel": "NET_Standard_2_1",
            "codeOptimization": "Release",
            "languageVersion": "9.0",
            "responseFiles": [],
            "roslynAdditionalFilePaths": [],
            "roslynAnalyzerDllPaths": [],
            "roslynAnalyzerRulesetPath": "",
        }
        receipt_assemblies = [{
            "name": assembly_name,
            "sourceFiles": [str(source_path)],
            "allReferences": [str(reference_path)],
            "compilerOptions": compiler_options,
            "defines": ["SYNTHETIC"],
            "flags": "None",
        }]
        receipt_outputs = []
        for raw_file in file_records:
            relative = f"{raw_file['assemblyName']}{raw_file['extension']}"
            artifact = next(item for item in artifacts if item["artifactId"] == raw_file["artifactId"])
            (artifact_directory / relative).write_bytes(Path(artifact["path"]).read_bytes())
            receipt_outputs.append({"relativePath": relative, "sha256": artifact["sha256"], "size": artifact["size"]})
        receipt = {
            "schema": "relay.liveloop.native-compile-input-receipt",
            "version": 1,
            "profileDigest": context["profileDigest"],
            "jobId": ticket.job_id,
            "inputSnapshot": ticket.input_snapshot,
            "projectRoot": self.project_root.as_posix(),
            "unityRoot": self.unity_root.as_posix(),
            "unityVersion": context["unityVersion"],
            "compileApi": "UnityEditor.Build.Player.PlayerBuildInterface.CompilePlayerScripts",
            "compileResultStatus": "SUCCESS",
            "typeDbPresent": True,
            "compilerProcessIdentityStatus": "NOT_PROVEN",
            "limitations": list(self.receipt_limitations) if self.receipt_limitations is not None else [
                "ACTUAL_COMPILER_PROCESS_IDENTITY_NOT_PROVEN",
                "ADDITIONAL_COMPILER_ARGUMENTS_MAY_REFERENCE_UNENUMERATED_FILES_NOT_PROVEN",
                "ANALYZER_OR_SOURCE_GENERATOR_TRANSITIVE_FILE_READS_NOT_PROVEN",
            ],
            "target": {
                "requested": context["buildTarget"],
                "activeBefore": context["buildTarget"],
                "activeAfter": context["buildTarget"],
                "group": context["buildTargetGroup"],
                "subtarget": context["subtarget"],
                "options": "DevelopmentBuild" if context["developmentBuild"] else "None",
                "developmentBuild": context["developmentBuild"],
                "extraScriptingDefines": context["extraScriptingDefines"],
                "extraScriptingDefinesWasNull": not context["extraScriptingDefines"],
            },
            "graphMatch": True,
            "assemblies": receipt_assemblies,
            "compileResultAssemblies": [str(artifact_directory / compile_attempt / f"{assembly_name}.dll")],
            "inputs": receipt_inputs,
            "inputSetSha256Before": native_compile_input_set_digest(receipt_inputs),
            "inputSetSha256After": native_compile_input_set_digest(receipt_inputs),
            "inputSetMatches": True,
            "outputs": receipt_outputs,
        }
        receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt_id = add_artifact("compile-input-receipt.json", "compile-input-receipt", "application/json", receipt_bytes)
        if self.mode == "noop":
            structure_equal = True
            changed_methods: list[dict[str, str]] = []
        elif self.mode == "hotfix":
            structure_equal = True
            changed_methods = [{
                "typeName": "SyntheticType",
                "signature": "System.Void SyntheticType::Apply()",
            }]
        else:
            structure_equal = False
            changed_methods = []
        assembly_analysis = [
            {
                "name": assembly["name"],
                "moduleId": assembly["moduleId"],
                "dependencies": assembly["dependencies"],
                "baselineSha256": assembly["baselineSha256"],
                "structureEqual": structure_equal,
                "changedMethods": changed_methods if assembly["moduleId"] == context["moduleId"] else [],
            }
            for assembly in context["assemblies"]
        ]
        analysis_id = f"editor-{ticket.job_id}-analysis"
        analysis = {
            "schema": "relay.liveloop.native-compile-analysis",
            "version": 3,
            "profileId": context["profileId"],
            "profileDigest": context["profileDigest"],
            "taskId": context["taskId"],
            "sessionId": context["sessionId"],
            "launchId": context["launchId"],
            "jobId": ticket.job_id,
            "inputSnapshot": ticket.input_snapshot,
            "analysisArtifactId": analysis_id,
            "compileManifestArtifactId": compile_manifest_id,
            "compileInputReceiptArtifactId": receipt_id,
            "compileInputReceiptSha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "files": file_records,
            "assemblies": assembly_analysis,
        }
        analysis_bytes = json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        analysis_item = {
            "artifactId": analysis_id,
            "kind": "native-compile-analysis",
            "path": str(artifact_directory / f"{analysis_id}.json"),
            "sha256": hashlib.sha256(analysis_bytes).hexdigest(),
            "mediaType": "application/json",
            "size": len(analysis_bytes),
        }
        Path(analysis_item["path"]).write_bytes(analysis_bytes)
        artifacts.append(analysis_item)
        result = {
            "jobId": ticket.job_id,
            "requestDigest": ticket.request_digest,
            "inputSnapshot": ticket.input_snapshot,
            "providerId": ticket.provider_id,
            "attemptId": "attempt-synthetic",
            "status": "completed",
            "completedAtUtc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "resultJson": analysis_bytes.decode("utf-8"),
            "error": None,
            "artifacts": artifacts,
        }
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temporary.replace(result_path)
        if self.source_to_mutate is not None:
            self.source_to_mutate.write_bytes(b"source-mutated-after-editor-compile")
            self.source_to_mutate = None
        if self.baseline_to_mutate is not None:
            self.baseline_to_mutate.write_bytes(b"baseline-mutated-after-editor-compile")
            self.baseline_to_mutate = None
        if self.task_to_mutate is not None and self.ledger is not None:
            self.ledger.update_task(self.task_to_mutate, {"goal": "Changed while Editor compile was running."})
            self.task_to_mutate = None


class SyntheticResourcePreparationProvider:
    provider_id = "synthetic-resource-preparation"
    is_verified = True
    unverified_reason = None

    def __init__(self, artifacts: ArtifactStore, root: Path, source_path: Path) -> None:
        self.artifacts = artifacts
        self.root = root
        self.source_path = source_path
        self.fail_build = False
        self.prepare_calls = 0

    def profile_for_task(self, _task: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(profile_id="synthetic-resource-profile")

    def current_input_snapshot(self, _task: dict[str, Any]) -> str:
        material = self.source_path.read_bytes()
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def current_profile_digest(self, _task: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(b"synthetic-resource-profile-v1").hexdigest()

    def prepare(self, task: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.prepare_calls += 1
        if self.fail_build:
            raise CommandError("RESOURCE_BUILD_FAILED", "Synthetic asset build failed before publishing a candidate.", stage="asset_build", runtime_changed=False)
        job_id = request["jobId"]
        state = request["runtimeState"]
        input_snapshot = self.current_input_snapshot(task)
        profile_digest = self.current_profile_digest(task)
        release_after = "release-synthetic-composite-2"
        context_requirements = {
            "schemaId": "synthetic.context",
            "schemaVersion": 1,
            "mediaType": "application/json",
        }
        affected_views: list[str] = []
        archive_bytes = b"synthetic immutable resource archive"
        directory = self.root / "resource-candidates" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        archive_path = directory / "resources.bundle"
        archive_path.write_bytes(archive_bytes)
        archive_id = f"resource-{job_id}-archive"
        archive = self.artifacts.register(
            archive_path,
            kind="resource_release_archive",
            artifact_id=archive_id,
            task_id=task["taskId"],
            job_id=job_id,
        )
        manifest = {
            "schema": "relay.liveloop.resource-release-candidate",
            "version": 1,
            "taskId": task["taskId"],
            "sessionId": task["sessionId"],
            "inputSnapshot": input_snapshot,
            "profileDigest": profile_digest,
            "runtimeRevisionBefore": state["runtimeRevision"],
            "resourceReleaseBefore": state["resourceRelease"],
            "resourceReleaseAfter": release_after,
            "archiveArtifactId": archive["artifactId"],
            "archiveSha256": archive["sha256"],
            "archiveSize": archive["size"],
            "contextRequirements": context_requirements,
            "affectedViews": affected_views,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_path = directory / "resource-release.json"
        manifest_path.write_bytes(manifest_bytes)
        manifest_id = f"resource-{job_id}-manifest"
        manifest_meta = self.artifacts.register(
            manifest_path,
            kind="resource_release_manifest",
            artifact_id=manifest_id,
            task_id=task["taskId"],
            job_id=job_id,
        )
        request_digest = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "inputSnapshot": input_snapshot,
            "profileDigest": profile_digest,
            "expectedRuntimeRevision": state["runtimeRevision"],
            "resourceReleaseBefore": state["resourceRelease"],
            "resourceReleaseAfter": release_after,
            "manifestArtifactId": manifest_id,
            "archiveArtifactId": archive_id,
            "artifactIds": [manifest_id, archive_id],
            "artifacts": [manifest_meta, archive],
            "requiredArtifactKinds": ["resource_release_manifest", "resource_release_archive"],
            "contextRequirements": context_requirements,
            "affectedViews": affected_views,
            "prepareComplete": True,
            "approvalRequired": False,
            "preparationEvidence": {
                "providerId": self.provider_id,
                "profileId": "synthetic-resource-profile",
                "editorJobId": f"asset-{job_id}",
                "editorRequestDigest": request_digest,
            },
        }

    def reconcile_prepare(self, _job: dict[str, Any], _task: dict[str, Any]) -> None:
        return None

    def verify_preparation_receipt(
        self,
        task: dict[str, Any],
        _evidence: dict[str, Any],
        *,
        expected_input_snapshot: str,
    ) -> None:
        if expected_input_snapshot != self.current_input_snapshot(task):
            raise CommandError("INPUT_CHANGED", "Synthetic resource source changed.", stage="asset_build", runtime_changed=False)


class NativeCompilePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-compile-preparation-")
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "synthetic-project"
        (self.project_root / "Assets").mkdir(parents=True)
        (self.project_root / "Baselines").mkdir()
        (self.project_root / "ProjectSettings").mkdir()
        (self.project_root / "Packages" / "com.code-philosophy.hybridclr").mkdir(parents=True)
        (self.project_root / "ProjectSettings" / "ProjectSettings.asset").write_text("synthetic settings", encoding="utf-8")
        (self.project_root / "Packages" / "manifest.json").write_text("{}", encoding="utf-8")
        (self.project_root / "Packages" / "packages-lock.json").write_text("{}", encoding="utf-8")
        (self.project_root / "Packages" / "com.code-philosophy.hybridclr" / "package.json").write_text("{}", encoding="utf-8")
        self.source_path = self.project_root / "Assets" / "Synthetic.cs"
        self.source_path.write_text("public sealed class SyntheticType { public void Apply() {} }", encoding="utf-8")
        self.reference_path = self.project_root / "Assets" / "ExtraReference.dll"
        self.reference_path.write_bytes(b"synthetic compiler reference omitted from sourceInputs")
        (self.project_root / "Assets" / "Synthetic.asmdef").write_text("{}", encoding="utf-8")
        (self.project_root / "Assets" / "Synthetic.asmdef.meta").write_text("fileFormatVersion: 2\nguid: synthetic-guid\n", encoding="utf-8")
        self.baseline_path = self.project_root / "Baselines" / "Synthetic.Assembly.dll"
        self.baseline_bytes = b"synthetic immutable baseline assembly v1"
        self.baseline_path.write_bytes(self.baseline_bytes)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.unity_root = self.root / "synthetic-unity"
        (self.unity_root / "Editor" / "Data" / "Tools" / "BuildPipeline").mkdir(parents=True)
        for relative in TOOLCHAIN_REQUIRED_RELATIVE:
            path = self.unity_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("synthetic toolchain " + relative).encode("utf-8"))
        (self.unity_root / "Editor" / "Data" / "Tools" / "BuildPipeline" / "BuildPipeline.dll").write_bytes(b"synthetic build pipeline")
        self.editor_artifact_root = self.artifact_root / "editor"
        self.job_root = self.root / "editor-jobs"
        self.profile_path = self.root / "native-compile-profiles.json"
        self._write_profile()
        self.profiles = NativeCompileProfileRegistry.load(self.profile_path)
        self.ledger = Ledger(self.root / "ledger.sqlite3")
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])
        self.player = FakeAuthenticatedPlayerTransport()
        self.session = RuntimeSessionConfig(
            SESSION_ID,
            LAUNCH_ID,
            self.player.runtime_revision,
            1,
            b"synthetic-only-secret-material-32-bytes",
            "127.0.0.1",
            0,
            RESOURCE_RELEASE,
        )
        self.editor = SyntheticEditorJobTransport(
            self.job_root,
            self.editor_artifact_root,
            self.project_root,
            self.unity_root,
            ledger=self.ledger,
        )
        self.preparation, self.runtime = create_native_update_providers(
            self.player,
            self.artifacts,
            self.session,
            ledger=self.ledger,
            profiles=self.profiles,
            editor_transport=self.editor,
        )
        self.service = build_command_service(
            self.ledger,
            self.artifacts,
            preparation_provider=self.preparation,
            runtime_provider=self.runtime,
        )
        self.request_count = 0

    def tearDown(self) -> None:
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _write_profile(self) -> None:
        self.profile_path.write_text(
            json.dumps(
                {
                    "schema": "relay.liveloop.native-compile-profiles",
                    "version": 2,
                    "profiles": [
                        {
                            "profileId": "synthetic-profile",
                            "target": "Synthetic target",
                            "projectRoot": str(self.project_root),
                            "unityRoot": str(self.unity_root),
                            "unityVersion": "2022.3.62f3",
                            "buildTarget": "StandaloneWindows64",
                            "buildTargetGroup": "Standalone",
                            "configuration": "Debug",
                            "developmentBuild": True,
                            "subtarget": 0,
                            "extraScriptingDefines": [],
                            "defines": ["SYNTHETIC"],
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
                                    "baselineSha256": hashlib.sha256(self.baseline_bytes).hexdigest(),
                                }
                            ],
                            "timeoutSeconds": 1,
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _command(self, operation: str, task_id: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
        self.request_count += 1
        envelope: dict[str, Any] = {
            "protocolVersion": 1,
            "requestId": f"compile-{operation.replace('.', '-')}-{self.request_count}",
            "operation": operation,
            "arguments": dict(arguments),
        }
        if task_id is not None:
            envelope["taskId"] = task_id
            envelope["arguments"].setdefault("taskId", task_id)
        return self.service.execute(envelope)

    def _open_task(self, route: str = "HOTFIX") -> str:
        response = self._command(
            "task.open",
            None,
            {
                "goal": "Exercise automatic source compile preparation.",
                "sessionId": SESSION_ID,
                "target": "Synthetic target",
                "allowedImpact": _impact(route),
                "acceptance": ["Synthetic compile candidate is bound to its source and baseline."],
            },
        )
        self.assertEqual("completed", response["status"], response)
        return response["result"]["taskId"]

    def _wait_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.ledger.get_job(job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail(f"job did not finish: {job_id}")

    def _wait_for_stage(self, job_id: str, stage: str, *, error_code: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.ledger.get_job(job_id)
            error = job.get("error") or {}
            if job["stage"] == stage and (error_code is None or error.get("code") == error_code):
                return job
            if job["state"] not in {"queued", "running"}:
                self.fail(f"job terminated before expected stage {stage}: {job}")
            time.sleep(0.01)
        self.fail(f"job did not reach stage {stage}: {job_id}")

    def _prepare(self, task_id: str) -> dict[str, Any]:
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"], accepted)
        return self._wait_job(accepted["jobId"])

    def _install_composite_provider(self, *, fail_build: bool = False) -> SyntheticResourcePreparationProvider:
        self.editor.mode = "module"
        source_path = self.project_root / "Assets" / "SyntheticResources.bin"
        source_path.write_bytes(b"synthetic resource inputs v1")
        resource_provider = SyntheticResourcePreparationProvider(self.artifacts, self.artifact_root, source_path)
        resource_provider.fail_build = fail_build
        composite = CompositePreparationProvider(self.preparation, resource_provider, self.artifacts, self.ledger)
        self.service.coordinator.preparation_provider = composite
        self.service.coordinator.preparation_verified = composite.is_verified
        return resource_provider

    def _durable_prepare_job(
        self, task_id: str, *, operation: str = "prepare", suffix: str = "default"
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        task = self.ledger.get_task(task_id)
        runtime_state = self.runtime.observe_task_state(task)
        profile = self.profiles.for_task(task)
        snapshot = native_input_snapshot(profile)
        job_id = f"job_compile_recover_{operation}_{suffix}"
        command = {
            "protocolVersion": 1,
            "requestId": f"recover-{operation}-request",
            "operation": operation,
            "taskId": task_id,
            "arguments": {"taskId": task_id},
        }
        job = self.ledger.create_job(
            {
                "jobId": job_id,
                "requestId": command["requestId"],
                "operation": operation,
                "taskId": task_id,
                "state": "running",
                "stage": "prepare_running",
                "runtimeChanged": False,
                "result": {"resumeCommand": command},
            }
        )
        binding = {
            "schema": "relay.liveloop.prepare-coordinator-binding",
            "version": 2,
            "jobId": job_id,
            "providerId": self.preparation.provider_id,
            "profileId": profile.profile_id,
            "profileDigest": native_compile_profile_digest(profile),
            "taskId": task_id,
            "sessionId": SESSION_ID,
            "taskUpdatedAt": task["updatedAt"],
            "inputSnapshot": snapshot,
            "runtimeState": runtime_state,
        }
        self.ledger.update_job(
            job_id,
            state="running",
            stage="prepare_running",
            runtime_changed=False,
            result={"resumeCommand": command, "prepareCoordinatorBinding": binding},
        )
        request = {
            "jobId": job_id,
            "taskId": task_id,
            "sessionId": SESSION_ID,
            "requestedInputSnapshot": None,
            "inputSnapshot": snapshot,
            "expectedRuntimeRevision": runtime_state["runtimeRevision"],
            "taskUpdatedAt": task["updatedAt"],
            "runtimeState": runtime_state,
            "profileId": profile.profile_id,
            "profileDigest": native_compile_profile_digest(profile),
        }
        return job_id, task, request

    def test_prepare_compiles_source_generates_bound_plan_and_iterate_reuses_it(self) -> None:
        task_id = self._open_task("HOTFIX")
        job = self._prepare(task_id)

        self.assertEqual("completed", job["state"], job)
        plan = job["result"]["plan"]
        self.assertEqual("HOTFIX", plan["route"])
        self.assertIsNone(self.ledger.get_task(task_id)["reference"], "prepare does not require an operator-supplied manifest")
        self.assertEqual(native_input_snapshot(self.profiles.for_task(self.ledger.get_task(task_id))), plan["inputSnapshot"])
        profile_digest = native_compile_profile_digest(self.profiles.for_task(self.ledger.get_task(task_id)))
        self.assertEqual(profile_digest, job["result"]["prepareBinding"]["profileDigest"])
        self.assertEqual(profile_digest, job["result"]["prepareCoordinatorBinding"]["profileDigest"])
        self.assertEqual(profile_digest, job["result"]["prepareProviderResult"]["profileDigest"])
        evidence = plan["details"]["preparationEvidence"]
        self.assertEqual(profile_digest, evidence["profileDigest"])
        self.assertEqual(job["jobId"], evidence["editorJobId"])
        receipt_metadata, receipt_stream = self.artifacts.open_verified(evidence["compileInputReceiptArtifactId"])
        try:
            receipt = json.loads(receipt_stream.read().decode("utf-8"))
        finally:
            receipt_stream.close()
        self.assertEqual("SUCCESS", receipt["compileResultStatus"])
        self.assertIs(receipt["typeDbPresent"], True)
        self.assertEqual(evidence["compileInputReceiptSha256"], receipt_metadata["sha256"])
        self.assertEqual("NOT_PROVEN", receipt["compilerProcessIdentityStatus"])
        self.assertEqual(COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, evidence["compilerInputCoverage"])
        self.assertEqual(receipt["limitations"], evidence["compilerInputLimitations"])
        self.assertEqual(REQUIRED_COMPILER_INPUT_LIMITATIONS, set(receipt["limitations"]))
        self.assertIn(evidence["analysisArtifactId"], evidence["editorCandidateArtifactIds"])
        self.assertIn(evidence["compileManifestArtifactId"], evidence["editorCandidateArtifactIds"])
        self.assertIn(evidence["runtimeManifestArtifactId"], {item["artifactId"] for item in plan["details"]["artifacts"]})
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual([True], self.editor.enqueue_submitted)
        self.assertTrue(self.editor.enqueue_bindings[0].get("prepareBinding"))
        self.assertTrue(self.editor.enqueue_bindings[0].get("editorRequest"))

        iterate = self._command("iterate", task_id, {"planId": plan["planId"]})
        self.assertEqual("accepted", iterate["status"], iterate)
        applied = self._wait_job(iterate["jobId"])
        self.assertEqual("completed", applied["state"], applied)
        self.assertEqual(["hotfix"], self.player.mutations)
        self.assertEqual(1, self.editor.wait_calls, "iterate must reuse the frozen plan without compiling again")
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual(COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, applied["result"]["compilerInputCoverage"])
        self.assertEqual(receipt["limitations"], applied["result"]["compilerInputLimitations"])
        self.assertEqual(evidence["compileInputReceiptArtifactId"], applied["result"]["compileInputReceiptArtifactId"])
        self.assertEqual(evidence["compileInputReceiptSha256"], applied["result"]["compileInputReceiptSha256"])

    def test_prepare_rejects_receipts_missing_nonhermetic_input_limitations(self) -> None:
        required = set(REQUIRED_COMPILER_INPUT_LIMITATIONS)
        for marker in (
            "ADDITIONAL_COMPILER_ARGUMENTS_MAY_REFERENCE_UNENUMERATED_FILES_NOT_PROVEN",
            "ANALYZER_OR_SOURCE_GENERATOR_TRANSITIVE_FILE_READS_NOT_PROVEN",
        ):
            with self.subTest(missing=marker):
                task_id = self._open_task("HOTFIX")
                self.editor.receipt_limitations = sorted(required - {marker})
                job = self._prepare(task_id)
                self.editor.receipt_limitations = None
                self.assertEqual("failed", job["state"], job)
                self.assertEqual("CONTRACT_MISMATCH", job["error"]["code"])
                with self.assertRaises(CommandError):
                    self.ledger.get_plan(self.service.coordinator._prepare_plan_id(job["jobId"]))
                self.assertFalse(self.player.mutations)

    def test_compile_diff_selects_module_reload_for_structural_change(self) -> None:
        self.editor.mode = "module"
        task_id = self._open_task("MODULE_RELOAD")
        job = self._prepare(task_id)
        self.assertEqual("completed", job["state"], job)
        plan = job["result"]["plan"]
        self.assertEqual("MODULE_RELOAD", plan["route"])
        self.assertEqual([MODULE_ID], plan["details"]["requiredImpact"]["reloadModules"])
        self.assertEqual(1, self.editor.compile_count)
        self.assertFalse(self.player.mutations)

    def test_compile_without_any_diff_fails_closed_without_plan(self) -> None:
        self.editor.mode = "noop"
        task_id = self._open_task("HOTFIX")
        job = self._prepare(task_id)
        self.assertEqual("failed", job["state"], job)
        self.assertEqual("RESOURCE_BUILD_FAILED", job["error"]["code"])
        self.assertEqual(1, self.editor.compile_count)
        with self.assertRaises(CommandError):
            self.ledger.get_plan(self.service.coordinator._prepare_plan_id(job["jobId"]))
        self.assertFalse(self.player.mutations)

    def test_source_task_or_baseline_change_during_editor_work_rejects_candidate(self) -> None:
        for case in ("source", "task", "baseline"):
            with self.subTest(case=case):
                task_id = self._open_task("HOTFIX")
                if case == "source":
                    self.editor.source_to_mutate = self.source_path
                elif case == "task":
                    self.editor.task_to_mutate = task_id
                else:
                    self.editor.baseline_to_mutate = self.baseline_path
                job = self._prepare(task_id)
                self.assertEqual("failed", job["state"], job)
                self.assertIn(job["error"]["code"], {"INPUT_CHANGED", "STALE_TARGET"})
                self.assertFalse(self.player.mutations)

    def test_compile_receipt_rechecks_reference_settings_and_toolchain_before_iterate(self) -> None:
        mutations = (
            ("compiler reference", self.reference_path),
            ("project setting", self.project_root / "ProjectSettings" / "ProjectSettings.asset"),
            ("Unity compiler", self.unity_root / "Editor" / "Data" / "MonoBleedingEdge" / "lib" / "mono" / "msbuild" / "Current" / "bin" / "Roslyn" / "csc.exe"),
        )
        for label, path in mutations:
            with self.subTest(input=label):
                task_id = self._open_task("HOTFIX")
                prepared = self._prepare(task_id)
                self.assertEqual("completed", prepared["state"], prepared)
                plan = prepared["result"]["plan"]
                original = path.read_bytes()
                path.write_bytes(original + b" changed after receipt sealing")
                try:
                    accepted = self._command("iterate", task_id, {"planId": plan["planId"]})
                    self.assertEqual("accepted", accepted["status"], accepted)
                    applied = self._wait_job(accepted["jobId"])
                    self.assertEqual("failed", applied["state"], applied)
                    self.assertEqual("INPUT_CHANGED", applied["error"]["code"])
                    self.assertFalse(self.player.mutations)
                finally:
                    path.write_bytes(original)

    def test_native_compile_profile_rejects_unknown_configuration(self) -> None:
        profile_document = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile_document["profiles"][0]["configuration"] = "Production"
        profile_document["profiles"][0]["developmentBuild"] = False
        with self.assertRaisesRegex(ValueError, "exactly Debug or Release"):
            NativeCompileProfileRegistry.from_value(profile_document)

    def test_restart_recovery_polls_exact_completed_editor_job_without_recompile(self) -> None:
        task_id = self._open_task("HOTFIX")
        self.editor.interrupt_after_result = True
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"], accepted)
        job_id = accepted["jobId"]
        before = self._wait_for_stage(job_id, "prepare_editor_wait", error_code="TIMEOUT")
        self.assertEqual("running", before["state"])
        self.assertEqual("prepare_editor_wait", before["stage"])
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual([True], self.editor.enqueue_submitted)

        recovered = self.service.coordinator.recover_pending()
        self.assertEqual([job_id], [item["jobId"] for item in recovered])
        after = recovered[0]
        self.assertEqual("completed", after["state"], after)
        self.assertEqual("prepared", after["stage"])
        self.assertIsNotNone(after["result"]["plan"]["planId"])
        self.assertEqual([True, False], self.editor.enqueue_submitted)
        self.assertEqual(1, self.editor.wait_calls, "recovery may poll but must not wait by dispatching a new compile")
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual([], self.player.mutations)

    def test_restart_with_no_editor_result_becomes_unknown_without_compile_replay(self) -> None:
        task_id = self._open_task("HOTFIX")
        self.editor.timeout_before_result = True
        accepted = self._command("prepare", task_id, {})
        self.assertEqual("accepted", accepted["status"], accepted)
        job_id = accepted["jobId"]
        self._wait_for_stage(job_id, "prepare_editor_wait", error_code="TIMEOUT")
        recovered = self.service.coordinator.recover_pending()
        self.assertEqual([job_id], [item["jobId"] for item in recovered])
        self.assertEqual("state_unknown", recovered[0]["state"], recovered[0])
        self.assertEqual("STATE_UNKNOWN", recovered[0]["error"]["code"])
        self.assertFalse(recovered[0]["error"]["details"].get("automaticCompileReplayAllowed", True))
        self.assertEqual([True, False], self.editor.enqueue_submitted)
        self.assertEqual(1, self.editor.wait_calls)
        self.assertEqual(0, self.editor.compile_count)
        self.assertEqual([], self.player.mutations)

    def test_archived_editor_attempt_with_missing_result_recovers_without_new_request(self) -> None:
        task_id = self._open_task("MODULE_RELOAD")
        job_id, task, request = self._durable_prepare_job(task_id, operation="prepare", suffix="archived-result-loss")
        profile = self.profiles.for_task(task)
        binding = self.preparation._make_binding(task, request, profile)
        envelope = self.preparation._make_envelope(job_id, task, profile, binding)
        self.preparation._save_job(
            job_id,
            "prepare_editor_dispatching",
            {"prepareBinding": binding, "editorRequest": envelope.as_dict(), "editorDispatchState": "dispatching"},
        )

        ticket = self.editor.enqueue(envelope)
        self.assertTrue(ticket.submitted)
        self.editor._complete(ticket)
        self.assertIsNotNone(self.editor.poll_result(ticket), "the first synthetic Editor attempt must publish a result before archival")
        processing_request = self.job_root / "processing" / f"{job_id}.request.json"
        self.assertTrue(processing_request.is_file())
        archive_request = self.job_root / "archive" / "requests" / f"{job_id}.request.json"
        archive_request.parent.mkdir(parents=True, exist_ok=True)
        processing_request.replace(archive_request)
        archived_attempt = self.job_root / "archive" / "attempts" / f"{job_id}.attempt-synthetic.json"
        archived_attempt.parent.mkdir(parents=True, exist_ok=True)
        archived_attempt.write_text(
            json.dumps(
                {
                    "jobId": job_id,
                    "requestDigest": ticket.request_digest,
                    "attemptId": "attempt-synthetic",
                    "status": "completed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        result_path = self.job_root / "results" / f"{job_id}.result.json"
        result_path.unlink()

        job = self.ledger.get_job(job_id)
        result_record = dict(job.get("result") or {})
        result_record.update(
            {
                "prepareBinding": binding,
                "editorRequest": envelope.as_dict(),
                "editorRequestDigest": ticket.request_digest,
                "editorSubmitted": True,
                "editorDispatchState": "submitted",
            }
        )
        self.ledger.update_job(
            job_id,
            state="running",
            stage="prepare_editor_wait",
            runtime_changed=False,
            result=result_record,
        )

        with self.assertRaises(CommandError) as raised:
            self.preparation.reconcile_prepare(self.ledger.get_job(job_id), task)

        self.assertEqual("STATE_UNKNOWN", raised.exception.code)
        self.assertFalse(raised.exception.details.get("automaticCompileReplayAllowed", True))
        self.assertFalse(raised.exception.details.get("newRequestWritten", True))
        self.assertTrue(archive_request.is_file())
        self.assertTrue(archived_attempt.is_file())
        self.assertTrue((self.job_root / "submissions" / f"{job_id}.submitted.json").is_file())
        self.assertFalse((self.job_root / "incoming" / f"{job_id}.request.json").exists())
        self.assertFalse((self.job_root / "processing" / f"{job_id}.request.json").exists())
        self.assertFalse(result_path.exists())
        self.assertEqual([True], self.editor.enqueue_submitted)
        self.assertEqual(1, self.editor.compile_count, "recovery did not execute a second synthetic BeginCompile")

    def test_recovered_inline_iterate_reuses_saved_compile_result_and_applies_same_plan(self) -> None:
        task_id = self._open_task("HOTFIX")
        job_id, task, request = self._durable_prepare_job(task_id, operation="iterate")
        self.preparation.prepare(task, request)
        prepared_state = self.ledger.get_job(job_id)
        self.assertEqual("prepare_candidate_ready", prepared_state["stage"])

        recovered = self.service.coordinator.recover_pending()
        self.assertEqual("completed", recovered[0]["state"], recovered[0])
        self.assertEqual("hotfix", self.player.mutations[0])
        self.assertEqual(self.service.coordinator._prepare_plan_id(job_id), recovered[0]["planId"])
        self.assertEqual(1, self.editor.wait_calls)
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual([True], self.editor.enqueue_submitted)

    def test_durable_envelope_rejects_changed_compile_profile_fields(self) -> None:
        task_id = self._open_task("HOTFIX")
        task = self.ledger.get_task(task_id)
        runtime_state = self.runtime.observe_task_state(task)
        profile = self.profiles.for_task(task)
        snapshot = native_input_snapshot(profile)
        request = {
            "jobId": "job_envelope_validation",
            "taskId": task_id,
            "sessionId": SESSION_ID,
            "inputSnapshot": snapshot,
            "requestedInputSnapshot": None,
            "expectedRuntimeRevision": runtime_state["runtimeRevision"],
            "taskUpdatedAt": task["updatedAt"],
            "runtimeState": runtime_state,
            "profileId": profile.profile_id,
            "profileDigest": native_compile_profile_digest(profile),
        }
        binding = self.preparation._make_binding(task, request, profile)
        envelope = self.preparation._make_envelope(request["jobId"], task, profile, binding)
        payload = json.loads(envelope.payload_json)
        payload["configuration"] = "Release"
        tampered = replace(envelope, payload_json=json.dumps(payload, separators=(",", ":")))
        with self.assertRaises(CommandError) as raised:
            self.preparation._verify_envelope_binding(tampered, request["jobId"], task, profile, binding)
        self.assertEqual("INPUT_CHANGED", raised.exception.code)

    def test_saved_prepare_result_rejects_same_id_profile_drift_without_compile_replay(self) -> None:
        def drift(profile: Any, field: str) -> Any:
            assembly = profile.assemblies[0]
            if field == "module_and_closure":
                changed_module = "synthetic-alternate-module"
                changed_assembly = replace(assembly, module_id=changed_module)
                return replace(
                    profile,
                    module_id=changed_module,
                    dependency_closure=(changed_module,),
                    assemblies=(changed_assembly,),
                )
            if field == "dependency_closure":
                return replace(profile, dependency_closure=(*profile.dependency_closure, "synthetic-extra-module"))
            if field == "entry_assembly":
                return replace(profile, entry_assembly_name="Synthetic.Alternate.Assembly")
            if field == "baseline_path":
                return replace(profile, assemblies=(replace(assembly, baseline_path="Baselines/Alternate.dll"),))
            if field == "baseline_hash":
                return replace(profile, assemblies=(replace(assembly, baseline_sha256="0" * 64),))
            raise AssertionError(field)

        for field in ("module_and_closure", "dependency_closure", "entry_assembly", "baseline_path", "baseline_hash"):
            with self.subTest(field=field):
                self.preparation._profiles = self.profiles
                task_id = self._open_task("HOTFIX")
                job_id, task, request = self._durable_prepare_job(task_id, operation="prepare", suffix=field)
                original_profile = self.profiles.for_task(task)
                original_snapshot = native_input_snapshot(original_profile)
                original_digest = native_compile_profile_digest(original_profile)
                compile_count_before = self.editor.compile_count
                self.preparation.prepare(task, request)

                job = self.ledger.get_job(job_id)
                saved = job["result"]["prepareProviderResult"]
                self.assertEqual("prepare_candidate_ready", job["stage"])
                self.assertEqual(original_digest, saved["profileDigest"])
                self.assertIsInstance(saved["preparationEvidence"], dict)

                changed_profile = drift(original_profile, field)
                self.assertEqual(original_snapshot, native_input_snapshot(changed_profile))
                self.assertNotEqual(original_digest, native_compile_profile_digest(changed_profile))
                self.preparation._profiles = NativeCompileProfileRegistry([changed_profile])

                with self.assertRaises(CommandError) as raised:
                    self.preparation.reconcile_prepare(job, task)
                self.assertEqual("INPUT_CHANGED", raised.exception.code)

                recovered = self.service.coordinator.recover_pending()
                self.assertEqual([job_id], [item["jobId"] for item in recovered])
                self.assertEqual("failed", recovered[0]["state"], recovered[0])
                self.assertEqual("INPUT_CHANGED", recovered[0]["error"]["code"])
                self.assertEqual(compile_count_before + 1, self.editor.compile_count)
                self.assertEqual([], self.player.mutations)

    def test_saved_prepare_result_requires_profile_digest_in_result_and_evidence(self) -> None:
        task_id = self._open_task("HOTFIX")
        job_id, task, request = self._durable_prepare_job(task_id, operation="prepare", suffix="result-digest")
        self.preparation.prepare(task, request)
        job = self.ledger.get_job(job_id)
        result_record = job["result"]
        profile = self.profiles.for_task(task)
        binding = result_record["prepareBinding"]
        saved = result_record["prepareProviderResult"]
        correct_digest = native_compile_profile_digest(profile)
        self.assertEqual(correct_digest, saved["profileDigest"])
        self.assertEqual(correct_digest, saved["preparationEvidence"]["profileDigest"])

        tampered_top_level = dict(saved)
        tampered_top_level["profileDigest"] = "sha256:" + "0" * 64
        tampered_evidence = dict(saved)
        tampered_evidence["preparationEvidence"] = dict(saved["preparationEvidence"])
        tampered_evidence["preparationEvidence"]["profileDigest"] = "sha256:" + "0" * 64
        for candidate in (tampered_top_level, tampered_evidence):
            with self.assertRaises(CommandError) as raised:
                self.preparation._verify_saved_provider_result(job_id, task, profile, binding, candidate)
            self.assertEqual("INPUT_CHANGED", raised.exception.code)

        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual([], self.player.mutations)

    def test_composite_resource_build_failure_creates_no_plan_or_player_mutation(self) -> None:
        resource_provider = self._install_composite_provider(fail_build=True)
        task_id = self._open_task("MODULE_AND_ASSET_RELOAD")
        job = self._prepare(task_id)

        self.assertEqual("failed", job["state"], job)
        self.assertEqual("RESOURCE_BUILD_FAILED", job["error"]["code"])
        self.assertIs(job["runtimeChanged"], False)
        with self.assertRaises(CommandError):
            self.ledger.get_plan(self.service.coordinator._prepare_plan_id(job["jobId"]))
        self.assertEqual(1, self.editor.compile_count)
        self.assertEqual(1, resource_provider.prepare_calls)
        self.assertEqual([], self.player.mutations)

    def test_composite_resource_tamper_is_rejected_before_player_mutation(self) -> None:
        self._install_composite_provider()
        task_id = self._open_task("MODULE_AND_ASSET_RELOAD")
        prepared = self._prepare(task_id)
        self.assertEqual("completed", prepared["state"], prepared)
        plan = prepared["result"]["plan"]
        archive_id = plan["details"]["compositeBinding"]["resource"]["archiveArtifactId"]
        archive_path = Path(self.ledger.get_artifact(archive_id)["absolutePath"])
        archive_path.write_bytes(b"tampered after immutable plan preparation")

        accepted = self._command("iterate", task_id, {"planId": plan["planId"]})
        self.assertEqual("accepted", accepted["status"], accepted)
        applied = self._wait_job(accepted["jobId"])

        self.assertEqual("failed", applied["state"], applied)
        self.assertEqual("INPUT_CHANGED", applied["error"]["code"])
        self.assertIs(applied["runtimeChanged"], False)
        self.assertEqual([], self.player.mutations)

    def test_composite_plan_binds_both_closures_and_uses_one_apply_sequence(self) -> None:
        self._install_composite_provider()
        task_id = self._open_task("MODULE_AND_ASSET_RELOAD")
        prepared = self._prepare(task_id)
        self.assertEqual("completed", prepared["state"], prepared)
        plan = prepared["result"]["plan"]
        binding = plan["details"]["compositeBinding"]
        code = binding["code"]
        resource = binding["resource"]

        self.assertEqual("MODULE_AND_ASSET_RELOAD", plan["route"])
        self.assertEqual(plan["inputSnapshot"], binding["inputSnapshot"])
        self.assertEqual(plan["details"]["preparationEvidence"]["profileDigest"], binding["profileDigest"])
        code_evidence = plan["details"]["preparationEvidence"]["code"]
        self.assertEqual(COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, code_evidence["compilerInputCoverage"])
        self.assertEqual(REQUIRED_COMPILER_INPUT_LIMITATIONS, set(code_evidence["compilerInputLimitations"]))
        self.assertEqual(artifact_closure_sha256(code["artifacts"]), code["artifactClosureSha256"])
        self.assertEqual(artifact_closure_sha256(resource["artifacts"]), resource["artifactClosureSha256"])
        self.assertEqual(
            set(code["artifactIds"]) | set(resource["artifactIds"]),
            {item["artifactId"] for item in plan["details"]["artifacts"]},
        )
        self.assertEqual(binding["runtimeState"]["resourceRelease"], resource["resourceReleaseBefore"])
        self.assertEqual(
            resource["resourceReleaseAfter"],
            plan["details"]["targetGenerationsAfter"]["resourceRelease"],
        )

        original_apply = self.runtime.apply
        apply_calls: list[tuple[str, str]] = []

        def counted_apply(candidate: dict[str, Any], job_id: str) -> dict[str, Any]:
            apply_calls.append((candidate["planId"], job_id))
            return original_apply(candidate, job_id)

        self.runtime.apply = counted_apply
        accepted = self._command("iterate", task_id, {"planId": plan["planId"]})
        self.assertEqual("accepted", accepted["status"], accepted)
        applied = self._wait_job(accepted["jobId"])

        self.assertEqual("completed", applied["state"], applied)
        self.assertEqual([(plan["planId"], accepted["jobId"])], apply_calls)
        self.assertEqual(
            ["module.quiesce", "module.dispose", "module.load", "resource.activate", "module.restore"],
            self.player.mutations,
        )
        self.assertEqual(COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, applied["result"]["compilerInputCoverage"])
        self.assertEqual(code_evidence["compilerInputLimitations"], applied["result"]["compilerInputLimitations"])
        self.assertEqual(code_evidence["compileInputReceiptArtifactId"], applied["result"]["compileInputReceiptArtifactId"])
        journal = applied["result"]["runtimeStageJournal"]
        self.assertEqual(self.player.mutations, [item["operation"] for item in journal])
        self.assertTrue(all(item["status"] == "acknowledged" for item in journal))
        self.assertIs(self.ledger.get_task(task_id)["facts"]["runtimeMatched"], True)

    def test_lost_resource_activation_acknowledgement_is_unknown_without_replay(self) -> None:
        self._install_composite_provider()
        task_id = self._open_task("MODULE_AND_ASSET_RELOAD")
        prepared = self._prepare(task_id)
        self.assertEqual("completed", prepared["state"], prepared)
        plan = prepared["result"]["plan"]
        self.player.lose_on_operation = "resource.activate"

        original_apply = self.runtime.apply
        apply_calls: list[str] = []

        def counted_apply(candidate: dict[str, Any], job_id: str) -> dict[str, Any]:
            apply_calls.append(candidate["planId"])
            return original_apply(candidate, job_id)

        self.runtime.apply = counted_apply
        accepted = self._command("iterate", task_id, {"planId": plan["planId"]})
        self.assertEqual("accepted", accepted["status"], accepted)
        applied = self._wait_job(accepted["jobId"])

        self.assertEqual("state_unknown", applied["state"], applied)
        self.assertIsNone(applied["runtimeChanged"])
        code_evidence = plan["details"]["preparationEvidence"]["code"]
        self.assertEqual(COMPILER_INPUT_COVERAGE_ENUMERATED_ONLY, applied["result"]["compilerInputCoverage"])
        self.assertEqual(code_evidence["compilerInputLimitations"], applied["result"]["compilerInputLimitations"])
        self.assertEqual(code_evidence["compileInputReceiptArtifactId"], applied["result"]["compileInputReceiptArtifactId"])
        self.assertEqual([plan["planId"]], apply_calls)
        self.assertEqual(
            ["module.quiesce", "module.dispose", "module.load", "resource.activate"],
            self.player.mutations,
        )
        journal = applied["result"]["runtimeStageJournal"]
        self.assertEqual(
            ["module.quiesce", "module.dispose", "module.load", "resource.activate"],
            [item["operation"] for item in journal],
        )
        self.assertEqual("acknowledged", journal[2]["status"])
        self.assertEqual("unknown", journal[3]["status"])
        self.assertFalse(applied["error"]["details"].get("automaticReplayAllowed", True))
        self.assertTrue(applied["error"]["details"].get("freshSessionRequired"))


if __name__ == "__main__":
    unittest.main()
