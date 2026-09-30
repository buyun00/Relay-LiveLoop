"""Synthetic server-owned external input roots through the existing normal provider."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path

from host.errors import CommandError
from host.native_compile_profile import NativeCompileProfileRegistry, native_compile_profile_digest
from host.native_compile_receipt import native_compile_input_set_digest, validate_native_compile_input_receipt
from tests import test_native_compile_preparation as preparation_fixture


class TrustedCompileSourceRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = preparation_fixture.NativeCompilePreparationTests(methodName="runTest")
        self.fixture.setUp()
        self.document = json.loads(self.fixture.profile_path.read_text(encoding="utf-8"))
        self.legacy_document = copy.deepcopy(self.document)
        self.external = self.fixture.root / "synthetic-shared-sources"
        (self.external / "Runtime" / "Core").mkdir(parents=True)
        self.paths = [f"Runtime/Core/Synthetic{i:02d}.cs" for i in range(19)]
        for relative in self.paths:
            (self.external / relative).write_bytes(("synthetic external " + relative).encode("utf-8"))
        self.document["version"] = 3
        self.document["profiles"][0]["trustedSourceRoots"] = [{
            "rootId": "shared-sources", "path": self.external.as_posix(), "inputs": list(reversed(self.paths)),
        }]
        self.install(self.document)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def install(self, document: dict) -> None:
        profiles = NativeCompileProfileRegistry.from_value(document)
        self.fixture.profiles = profiles
        self.fixture.preparation._profiles = profiles
        self.profile = profiles.for_task({"target": "Synthetic target"})

    def prepared(self) -> tuple[str, dict, dict, Path]:
        task_id = self.fixture._open_task()
        job = self.fixture._prepare(task_id)
        self.assertEqual("completed", job["state"], job)
        receipt_id = job["result"]["plan"]["details"]["preparationEvidence"]["compileInputReceiptArtifactId"]
        record = self.fixture.ledger.get_artifact(receipt_id)
        path = Path(record["absolutePath"])
        return task_id, job, json.loads(path.read_bytes()), path

    def validate(self, receipt: dict, path: Path) -> None:
        validate_native_compile_input_receipt(receipt, self.profile, job_id=receipt["jobId"], receipt_path=path)

    @staticmethod
    def reseal_inputs(receipt: dict) -> None:
        digest = native_compile_input_set_digest(receipt["inputs"])
        receipt["inputSetSha256Before"] = digest
        receipt["inputSetSha256After"] = digest

    def rejects(self, receipt: dict, path: Path, code: str = "CONTRACT_MISMATCH") -> None:
        with self.assertRaises(CommandError) as raised:
            self.validate(receipt, path)
        self.assertEqual(code, raised.exception.code)
        self.assertIs(raised.exception.runtime_changed, False)

    def test_normal_factory_binds_all_19_actual_external_files(self) -> None:
        _task, job, receipt, path = self.prepared()
        self.assertEqual(3, self.profile.profile_version)
        payload = json.loads(job["result"]["editorRequest"]["payloadJson"])
        context = json.loads(payload["providerContextJson"])
        self.assertEqual(4, context["version"])
        self.assertEqual(2, receipt["version"])
        self.assertEqual(self.profile.context_source_roots(), context["trustedSourceRoots"])
        self.assertEqual(context["trustedSourceRoots"], receipt["trustedSourceRoots"])
        external = [row for row in receipt["inputs"] if row["scope"] == "source:shared-sources"]
        self.assertEqual(set(self.paths), {row["path"] for row in external})
        for row in external:
            self.assertEqual(hashlib.sha256((self.external / row["path"]).read_bytes()).hexdigest(), row["sha256"])
        self.validate(receipt, path)
        self.assertEqual(1, self.fixture.editor.compile_count)
        self.assertEqual([], self.fixture.player.mutations)

    def test_legacy_v2_digest_and_context_are_byte_compatible(self) -> None:
        # Freeze the legacy serialized contract from server configuration, independently of the new DTO.
        legacy = NativeCompileProfileRegistry.from_value(self.legacy_document).for_task({"target": "Synthetic target"})
        digest_doc = dict(self.legacy_document["profiles"][0], schema="relay.liveloop.native-compile-profile/2", version=2)
        digest_doc["projectRoot"] = legacy.project_root.as_posix()
        digest_doc["unityRoot"] = legacy.unity_root.as_posix()
        digest_doc["timeoutSeconds"] = legacy.timeout_seconds
        expected = "sha256:" + hashlib.sha256(json.dumps(digest_doc, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(expected, native_compile_profile_digest(legacy))
        self.install(self.legacy_document)
        _task, job, receipt, path = self.prepared()
        context = json.loads(json.loads(job["result"]["editorRequest"]["payloadJson"])["providerContextJson"])
        self.assertEqual(3, context["version"])
        self.assertEqual(1, receipt["version"])
        self.assertNotIn("trustedSourceRoots", context)
        self.assertNotIn("trustedSourceRoots", receipt)
        self.validate(receipt, path)

    def test_configuration_normalizes_order_and_binds_root_identity_and_file_set(self) -> None:
        self.assertEqual(self.paths, list(self.profile.trusted_source_roots[0].inputs))
        original_digest = native_compile_profile_digest(self.profile)
        ordered = copy.deepcopy(self.document)
        ordered["profiles"][0]["trustedSourceRoots"][0]["inputs"] = self.paths
        self.install(ordered)
        self.assertEqual(original_digest, native_compile_profile_digest(self.profile))
        for change in ("rootId", "inputs"):
            candidate = copy.deepcopy(ordered)
            root = candidate["profiles"][0]["trustedSourceRoots"][0]
            root[change] = "another-source" if change == "rootId" else self.paths[:-1]
            self.install(candidate)
            self.assertNotEqual(original_digest, native_compile_profile_digest(self.profile))

    def test_configuration_refuses_missing_reserved_duplicate_or_overlapping_roots(self) -> None:
        variants = []
        missing = copy.deepcopy(self.document)
        del missing["profiles"][0]["trustedSourceRoots"]
        variants.append(missing)
        for change in ("reserved", "duplicate", "project", "parent", "volume", "relative", "dot-alias", "missing-file", "unexpected-field"):
            candidate = copy.deepcopy(self.document)
            roots = candidate["profiles"][0]["trustedSourceRoots"]
            if change == "reserved": roots[0]["rootId"] = "UNITY"
            elif change == "duplicate": roots.append(dict(roots[0], rootId="SHARED-SOURCES"))
            elif change == "project": roots[0]["path"] = self.fixture.project_root.as_posix()
            elif change == "parent": roots[0]["path"] = self.fixture.root.as_posix()
            elif change == "volume": roots[0]["path"] = self.external.anchor.replace("\\", "/")
            elif change == "relative": roots[0]["path"] = "synthetic-relative-root"
            elif change == "dot-alias": roots[0]["path"] = self.external.as_posix() + "/../synthetic-shared-sources"
            elif change == "missing-file": roots[0]["inputs"] = ["Runtime/Missing.cs"]
            else: roots[0]["wildcard"] = "*"
            variants.append(candidate)
        v2_with_roots = copy.deepcopy(self.document)
        v2_with_roots["version"] = 2
        variants.append(v2_with_roots)
        for field in ("sourceInputs", "baselinePath"):
            candidate = copy.deepcopy(self.document)
            if field == "sourceInputs":
                candidate["profiles"][0][field] = ["Assets/../Assets/Synthetic.cs"]
            else:
                candidate["profiles"][0]["assemblies"][0][field] = "Baselines/../Baselines/Synthetic.Assembly.dll"
            variants.append(candidate)
        for candidate in variants:
            with self.subTest(candidate=candidate["version"], roots=candidate["profiles"][0].get("trustedSourceRoots")):
                with self.assertRaises((ValueError, OSError)):
                    NativeCompileProfileRegistry.from_value(candidate)

    def test_configuration_refuses_relative_aliases_duplicates_and_hardlinks(self) -> None:
        for path in (".", "Runtime/../Core/File.cs", "Runtime//Core/File.cs", "/Synthetic.cs", "Runtime\\Core\\Synthetic00.cs", "Runtime/Core/Synthetic00.cs:"):
            candidate = copy.deepcopy(self.document)
            candidate["profiles"][0]["trustedSourceRoots"][0]["inputs"] = [path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                NativeCompileProfileRegistry.from_value(candidate)
        duplicate = copy.deepcopy(self.document)
        duplicate["profiles"][0]["trustedSourceRoots"][0]["inputs"].append(self.paths[0].upper())
        with self.assertRaises(ValueError):
            NativeCompileProfileRegistry.from_value(duplicate)
        alias = self.external / "Runtime" / "HardLink.cs"
        os.link(self.external / self.paths[0], alias)
        try:
            with self.assertRaisesRegex(ValueError, "hard-link"):
                NativeCompileProfileRegistry.from_value(self.document)
        finally:
            alias.unlink()

    def test_reparse_alias_rejected_at_configuration_and_revalidation(self) -> None:
        target = self.fixture.root / "synthetic-linked-directory"
        target.mkdir()
        (target / "Synthetic00.cs").write_bytes(b"synthetic linked contents")
        core = self.external / "Runtime" / "Core"
        original = self.external / "Runtime" / "OriginalCore"
        _task, _job, receipt, receipt_path = self.prepared()
        core.rename(original)
        try:
            if os.name == "nt":
                created = subprocess.run(["cmd", "/c", "mklink", "/J", str(core), str(target)],
                    capture_output=True, check=False)
                self.assertEqual(0, created.returncode, created.stdout + created.stderr)
            else:
                os.symlink(target, core, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "reparse"):
                NativeCompileProfileRegistry.from_value(self.document)
            self.rejects(receipt, receipt_path)
        finally:
            if core.is_symlink(): core.unlink()
            elif core.exists(): os.rmdir(core)  # only this owned junction, never recursive target deletion
            original.rename(core)

    def test_clients_cannot_install_or_override_trusted_roots(self) -> None:
        task_id = self.fixture._open_task()
        rejected = self.fixture._command("prepare", task_id, {"trustedSourceRoots": self.profile.context_source_roots()})
        self.assertEqual("failed", rejected["status"])
        self.assertEqual("CONTRACT_MISMATCH", rejected["error"]["code"])
        self.assertEqual(0, self.fixture.editor.compile_count)

    def test_durable_context_root_or_version_drift_rejected_without_replay(self) -> None:
        task_id = self.fixture._open_task()
        _job_id, task, request = self.fixture._durable_prepare_job(task_id)
        binding = self.fixture.preparation._make_binding(task, request, self.profile)
        envelope = self.fixture.preparation._make_envelope(request["jobId"], task, self.profile, binding)
        for change in ("identity", "path", "inputs", "version"):
            payload = json.loads(envelope.payload_json)
            context = json.loads(payload["providerContextJson"])
            if change == "identity": context["trustedSourceRoots"][0]["rootId"] = "wrong-source"
            elif change == "path": context["trustedSourceRoots"][0]["path"] = self.fixture.project_root.as_posix()
            elif change == "inputs": context["trustedSourceRoots"][0]["inputs"] = []
            else: context["version"] = 3
            payload["providerContextJson"] = json.dumps(context)
            tampered = replace(envelope, payload_json=json.dumps(payload))
            with self.subTest(change=change), self.assertRaises(CommandError) as raised:
                self.fixture.preparation._verify_envelope_binding(tampered, request["jobId"], task, self.profile, binding)
            self.assertEqual("INPUT_CHANGED", raised.exception.code)
        self.assertEqual(0, self.fixture.editor.compile_count)

    def test_receipt_root_binding_version_and_wrong_attribution_rejected(self) -> None:
        _task, _job, original, path = self.prepared()
        for change in ("version", "rootId", "rootPath", "rootFiles", "scope", "scope-type"):
            receipt = copy.deepcopy(original)
            if change == "version": receipt["version"] = 1
            elif change == "rootId": receipt["trustedSourceRoots"][0]["rootId"] = "wrong-source"
            elif change == "rootPath": receipt["trustedSourceRoots"][0]["path"] = self.fixture.project_root.as_posix()
            elif change == "rootFiles": receipt["trustedSourceRoots"][0]["inputs"].pop()
            else:
                external = next(row for row in receipt["inputs"] if row["scope"].startswith("source:"))
                external["scope"] = "project" if change == "scope" else []
                if change == "scope": self.reseal_inputs(receipt)
            with self.subTest(change=change): self.rejects(receipt, path)

    def test_missing_real_input_or_additional_input_role_rejected_even_when_resealed(self) -> None:
        _task, _job, original, path = self.prepared()
        extra = self.external / "Runtime" / "NotConfigured.cs"
        extra.write_bytes(b"synthetic not in closed file set")
        for change in ("missing", "extra-external", "extra-project", "extra-role"):
            receipt = copy.deepcopy(original)
            external = next(row for row in receipt["inputs"] if row["scope"].startswith("source:"))
            if change == "missing": receipt["inputs"].remove(external)
            elif change == "extra-role": external["roles"].append("unverified-extra-role")
            elif change == "extra-external":
                receipt["inputs"].append(dict(external, path="Runtime/NotConfigured.cs", sha256=hashlib.sha256(extra.read_bytes()).hexdigest(), size=extra.stat().st_size))
            else:
                source = self.fixture.baseline_path
                receipt["inputs"].append(dict(scope="project", path=source.relative_to(self.fixture.project_root).as_posix(),
                    roles=["assembly-source:Synthetic.Assembly"], sha256=hashlib.sha256(source.read_bytes()).hexdigest(), size=source.stat().st_size))
            self.reseal_inputs(receipt)
            with self.subTest(change=change): self.rejects(receipt, path)

    def test_graph_path_alias_escape_missing_and_unconfigured_real_input_rejected(self) -> None:
        _task, _job, original, path = self.prepared()
        extra = self.external / "Runtime" / "NotConfigured.cs"
        extra.write_bytes(b"synthetic actual extra compiler input")
        for bad in (self.external.as_posix() + "/Runtime/../Runtime/Core/Synthetic00.cs",
                    (self.fixture.root / "outside.cs").as_posix(), (self.external / "Missing.cs").as_posix(), extra.as_posix()):
            receipt = copy.deepcopy(original)
            receipt["assemblies"][0]["sourceFiles"].append(bad)
            with self.subTest(path=bad): self.rejects(receipt, path)

    def test_before_after_or_recorded_hash_drift_is_not_exempted(self) -> None:
        _task, _job, original, path = self.prepared()
        receipt = copy.deepcopy(original)
        receipt["inputSetSha256After"] = "0" * 64
        self.rejects(receipt, path)
        receipt = copy.deepcopy(original)
        external = next(row for row in receipt["inputs"] if row["scope"].startswith("source:"))
        external["sha256"] = "0" * 64
        self.reseal_inputs(receipt)
        self.rejects(receipt, path, "INPUT_CHANGED")

    def test_missing_or_changed_actual_external_file_fails_before_runtime_mutation(self) -> None:
        task_id, job, receipt, receipt_path = self.prepared()
        path = self.external / self.paths[0]
        original = path.read_bytes()
        path.unlink()
        self.rejects(receipt, receipt_path)
        path.write_bytes(original + b" changed after seal")
        accepted = self.fixture._command("iterate", task_id, {"planId": job["result"]["plan"]["planId"]})
        self.assertEqual("accepted", accepted["status"], accepted)
        applied = self.fixture._wait_job(accepted["jobId"])
        self.assertEqual("failed", applied["state"], applied)
        self.assertEqual("INPUT_CHANGED", applied["error"]["code"])
        self.assertEqual([], self.fixture.player.mutations)
        self.assertEqual(1, self.fixture.editor.compile_count)


if __name__ == "__main__":
    unittest.main()
