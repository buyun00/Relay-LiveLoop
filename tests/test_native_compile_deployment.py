"""Compile profile relocation checks; no real Editor, Player, or session is created."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.native_compile_deployment import export_plan, inspect, reconstruct
from tests import test_native_compile_preparation as preparation_fixture


class NativeCompileDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.NativeCompilePreparationTests(methodName="runTest")
        self.fixture.setUp()
        f = self.fixture
        self.external = f.root / "shared-sources"
        self.external.mkdir()
        self.inputs = ["Synthetic%02d.cs" % i for i in range(19)]
        for relative in self.inputs:
            (self.external / relative).write_text("synthetic source " + relative, encoding="utf-8")
        self.document = json.loads(f.profile_path.read_text())
        self.document["version"] = 3
        raw = self.document["profiles"][0]
        raw["projectRoot"] = f.project_root.as_posix()
        raw["unityRoot"] = f.unity_root.as_posix()
        raw["references"] = ["Assets/ExtraReference.dll", (f.unity_root / "Editor/Unity.exe").as_posix()]
        raw["trustedSourceRoots"] = [{"rootId": "shared", "path": self.external.as_posix(), "inputs": self.inputs}]
        f.profile_path.write_text(json.dumps(self.document), encoding="utf-8")
        self.config = {"gameProjectRoot": str(f.project_root), "unityExecutable": str(f.unity_root / "Editor/Unity.exe"),
            "dataRoot": str(f.root / "new-data"), "nativeCompile": {"profilesFile": str(f.profile_path),
            "editorJobRoot": str(f.job_root), "editorArtifactRoot": str(f.editor_artifact_root)}}
        Path(self.config["dataRoot"]).mkdir()

    def tearDown(self):
        self.fixture.tearDown()

    def transfer(self):
        plan = export_plan(self.config)
        for row in plan["sources"]:
            path = Path(self.config["dataRoot"]) / row["relativePath"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(row["sourcePath"], path)
        return plan["portable"]

    def test_v3_reconstructs_closed_roots_and_new_digest_without_session(self):
        before = inspect(self.config)
        portable = self.transfer()
        serialized = json.dumps(portable)
        self.assertNotIn(str(self.fixture.project_root), serialized)
        self.assertNotIn(self.external.as_posix(), serialized)
        result = reconstruct(portable, self.config, Path(self.config["dataRoot"]) / "profiles.json")
        after = result["validation"]
        self.assertEqual((4, 2, 19), tuple(after["profiles"][0][k] for k in ("contextVersion", "receiptVersion", "trustedSourceInputCount")))
        self.assertNotEqual(before["profiles"][0]["profileDigest"], after["profiles"][0]["profileDigest"])
        self.assertFalse(result["runtimeSessionImported"])
        self.assertFalse(result["secondMachineValidated"])
        self.assertFalse(after["nativeRuntimeVerified"])
        restored = json.loads(Path(result["nativeCompile"]["profilesFile"]).read_text())
        self.assertEqual(self.document["profiles"][0]["assemblies"], restored["profiles"][0]["assemblies"])
        self.assertEqual(self.inputs, restored["profiles"][0]["trustedSourceRoots"][0]["inputs"])

    def test_project_input_drift_rejects_before_profile_publication(self):
        portable = self.transfer()
        self.fixture.source_path.write_text("changed source")
        destination = Path(self.config["dataRoot"]) / "profiles.json"
        with self.assertRaises(ValueError): reconstruct(portable, self.config, destination)
        self.assertFalse(destination.exists())

    def test_reference_drift_rejects_before_profile_publication(self):
        portable = self.transfer()
        self.fixture.reference_path.write_bytes(b"changed reference")
        with self.assertRaises(ValueError): reconstruct(portable, self.config, Path(self.config["dataRoot"]) / "profiles.json")

    def test_missing_local_baseline_is_not_imported_or_substituted(self):
        portable = self.transfer()
        self.fixture.baseline_path.unlink()
        with self.assertRaises(OSError): reconstruct(portable, self.config, Path(self.config["dataRoot"]) / "profiles.json")

    def test_projection_cannot_install_arbitrary_absolute_source_root(self):
        portable = self.transfer()
        portable["profiles"][0]["trustedSourceRoots"][0]["path"] = self.external.as_posix()
        with self.assertRaises(ValueError): reconstruct(portable, self.config, Path(self.config["dataRoot"]) / "profiles.json")

    def test_unknown_absolute_reference_is_not_exported(self):
        other = self.fixture.root / "unknown.dll"
        other.write_bytes(b"synthetic")
        document = copy.deepcopy(self.document)
        document["profiles"][0]["references"].append(str(other))
        self.fixture.profile_path.write_text(json.dumps(document))
        with self.assertRaises(ValueError): export_plan(self.config)

    @unittest.skipUnless(sys.platform == "win32", "Windows case aliases")
    def test_installed_reference_casing_binds_actual_file_identity(self):
        self.document["profiles"][0]["references"][1] = (self.fixture.unity_root / "Editor/unity.exe").as_posix()
        self.fixture.profile_path.write_text(json.dumps(self.document))
        portable = export_plan(self.config)["portable"]
        self.assertEqual("Editor/Unity.exe", portable["profiles"][0]["references"][1]["path"])

    def test_profile_must_match_machine_project(self):
        alternate = self.fixture.root / "other-project"
        alternate.mkdir()
        self.config["gameProjectRoot"] = str(alternate)
        with self.assertRaises(ValueError): inspect(self.config)

    def test_existing_profile_cannot_be_overwritten(self):
        portable = self.transfer()
        path = Path(self.config["dataRoot"]) / "profiles.json"
        path.write_bytes(b"preserved")
        with self.assertRaises(ValueError): reconstruct(portable, self.config, path)
        self.assertEqual(b"preserved", path.read_bytes())

    def test_legacy_v2_projection_preserves_absent_root_contract(self):
        document = copy.deepcopy(self.document)
        document["version"] = 2
        del document["profiles"][0]["trustedSourceRoots"]
        self.fixture.profile_path.write_text(json.dumps(document))
        portable = export_plan(self.config)["portable"]
        result = reconstruct(portable, self.config, Path(self.config["dataRoot"]) / "profiles.json")
        self.assertEqual((3, 1, 0), tuple(result["validation"]["profiles"][0][k] for k in ("contextVersion", "receiptVersion", "trustedSourceInputCount")))
        restored = json.loads(Path(result["nativeCompile"]["profilesFile"]).read_text())
        self.assertNotIn("trustedSourceRoots", restored["profiles"][0])

    @unittest.skipUnless(sys.platform == "win32", "Windows deployment scripts")
    def test_bootstrap_publishes_valid_profile3_and_start_preview_passes_all_native_flags(self):
        f = self.fixture
        (f.project_root / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.99f1\n")
        token = f.root / "synthetic-token.txt"
        token.write_text("synthetic token")
        session = f.root / "synthetic-session.json"
        session.write_text("{}")
        config = f.root / "deployment-machine.json"
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(scripts / "bootstrap.ps1"),
            "-ConfigPath", str(config), "-ToolRepoRoot", str(scripts.parent), "-GameRepoRoot", str(f.project_root),
            "-GameProjectRoot", str(f.project_root), "-DataRoot", self.config["dataRoot"], "-PythonExecutable", sys.executable,
            "-UnityExecutable", self.config["unityExecutable"], "-TokenFile", str(token),
            "-NativeCompileProfilesFile", str(f.profile_path), "-EditorJobRoot", str(f.job_root),
            "-EditorArtifactRoot", str(f.editor_artifact_root), "-RuntimeSessionFile", str(session)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        actual = json.loads(config.read_text())
        self.assertEqual(str(f.profile_path), actual["nativeCompile"]["profilesFile"])
        # Read the same argument builder used by start, without starting a process.
        common = scripts / "lib/Deployment.Common.ps1"
        command = ". '%s'; $c=Get-RelayMachineConfig -ConfigPath '%s'; ConvertTo-Json -InputObject @(Get-RelayNativeCompileArguments -Config $c) -Compress" % (str(common), str(config))
        arguments = subprocess.run(["pwsh", "-NoProfile", "-Command", command],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(0, arguments.returncode, arguments.stderr)
        flags = json.loads(arguments.stdout)
        for flag in ("--runtime-session-file", "--native-compile-profiles", "--editor-job-root", "--editor-artifact-root"):
            self.assertEqual(1, flags.count(flag))

    @unittest.skipUnless(sys.platform == "win32", "Windows deployment scripts")
    def test_portable_scripts_rebuild_exact_native_roots_and_exclude_session(self):
        f = self.fixture
        tool = f.root / "tool"
        (tool / ".git").mkdir(parents=True)
        (tool / "relay_liveloop.py").write_text("# synthetic checkout marker")
        (f.project_root / ".git").mkdir()
        (f.project_root / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.99f1\n")
        data = Path(self.config["dataRoot"])
        for relative in ("baselines", "resources", "deployment/process-state", "host", "artifacts"):
            (data / relative).mkdir(parents=True, exist_ok=True)
        (data / "baselines/assembly.bin").write_bytes(b"synthetic immutable baseline selection")
        (data / "resources/resource.bin").write_bytes(b"synthetic resource selection")
        project_config = f.project_root / "project.json"
        project_config.write_text('{"projectId":"synthetic-project"}')
        token = f.root / "machine-token.txt"
        token.write_text("synthetic-private-token")
        session = f.root / "runtime-session.json"
        session.write_text('{"syntheticSecret":"must-not-be-exported"}')
        machine = dict(self.config, configKind="relay-liveloop-machine", schemaVersion=1,
            toolRepoRoot=str(tool), gameRepoRoot=str(f.project_root), tokenFile=str(token),
            pythonExecutable=sys.executable, controlAddress="127.0.0.1", controlPort=28480, runtimePort=28481,
            unityProjectVersion="2022.3.99f1", sdkRoots=[], interactiveDesktopVerified=False,
            renderAfterRdpDisconnect="UNVERIFIED", runtimeSessionFile=str(session),
            lifecycle={"stateRoot":str(data / "deployment/process-state"), "databasePath":str(data / "host/ledger.sqlite3"),
                       "artifactRoots":[str(data / "artifacts")]})
        config = f.root / "machine.json"
        config.write_text(json.dumps(machine))
        script = Path(__file__).resolve().parents[1] / "scripts/portable.ps1"
        archive = data / "transfer.zip"
        def invoke(arguments):
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script), *arguments],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)
        export = invoke(["Export", "-ConfigPath", str(config), "-ArchivePath", str(archive),
            "-ProjectConfigPath", str(project_config), "-DependencyLockPath", str(f.project_root / "Packages/packages-lock.json"),
            "-BaselinePath", str(data / "baselines"), "-ResourcePath", str(data / "resources")])
        self.assertFalse(export["exportedCredentialsOrLicenses"])
        import zipfile
        with zipfile.ZipFile(archive) as zipped:
            manifest = json.loads(zipped.read("manifest.json"))
            self.assertEqual(2, manifest["schemaVersion"])
            self.assertEqual(19, len([x for x in manifest["entries"] if x["category"] == "nativeSource"]))
            self.assertFalse(any("runtime-session" in x or "Library/" in x for x in zipped.namelist()))
        # A synthetic checkout with matching local inputs represents a separate installation.
        # No real Unity project or Library is copied or launched.
        new_root = f.root / "relocated-machine"
        new_tool = new_root / "tool"
        (new_tool / ".git").mkdir(parents=True)
        (new_tool / "relay_liveloop.py").write_text("# synthetic checkout marker")
        new_project = new_root / "game"
        shutil.copytree(f.project_root, new_project)
        new_unity = new_root / "installed-unity"
        (new_unity / "Editor").mkdir(parents=True)
        shutil.copyfile(f.unity_root / "Editor/Unity.exe", new_unity / "Editor/Unity.exe")
        new_token = f.root / "new-private-token.txt"
        new_token.write_text("synthetic-new-machine-token")
        destination = f.root / "relocated-data"
        new_config = f.root / "new-machine.json"
        result = invoke(["Import", "-ConfigPath", str(new_config), "-ArchivePath", str(archive),
            "-DiscoveryRoot", str(new_root), "-MaxDiscoveryDepth", "1", "-NewDataRoot", str(destination),
            "-PythonExecutable", sys.executable, "-UnityExecutable", str(new_unity / "Editor/Unity.exe"),
            "-TokenFile", str(new_token)])
        self.assertEqual(19, result["nativeCompileConfiguration"]["profiles"][0]["trustedSourceInputCount"])
        migrated = json.loads(new_config.read_text())
        self.assertNotIn("runtimeSessionFile", migrated)
        self.assertEqual("NOT_RUN", migrated["migration"]["newMachineValidation"])
        self.assertEqual("synthetic-new-machine-token", new_token.read_text())
