from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


configured_public_root = os.environ.get("RELAY_LIVELOOP_SUBJECT")
if not configured_public_root:
    raise RuntimeError("Set RELAY_LIVELOOP_SUBJECT to the public repository root for focused composition tests.")
PUBLIC_ROOT = Path(configured_public_root).resolve()
SOURCE_ROOT = PUBLIC_ROOT / "host"
if str(PUBLIC_ROOT) not in sys.path:
    sys.path.insert(0, str(PUBLIC_ROOT))


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


provider_module = importlib.import_module("host.player_process_provider")
composition_module = importlib.import_module("host.player_process_composition")

from host.artifacts import ArtifactStore
from host.ledger import Ledger


class _Transport:
    authenticated = True

    def connection_state(self) -> dict[str, Any]:
        return {
            "transportAuthenticated": True,
            "sessionId": "session-composition-1",
            "launchId": "launch-composition-1",
            "expectedRuntimeRevision": "runtime-composition-1",
            "nativeCapabilitiesVerified": False,
        }


class PlayerProcessCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="player-process-composition-")
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()
        self.session_file = self.data_root / "runtime-session.json"
        self.session_file.write_text(
            json.dumps(
                {
                    "sessionId": "session-composition-1",
                    "launchId": "launch-composition-1",
                    "runtimeRevision": "runtime-composition-1",
                    "sharedSecretBase64": base64.b64encode(b"composition-secret-fixture-32-bytes!!").decode("ascii"),
                    "protocolVersion": 1,
                    "hostAddress": "127.0.0.1",
                    "port": 0,
                }
            ),
            encoding="utf-8",
        )
        self.database_path = self.data_root / "ledger.sqlite3"
        self.ledger = Ledger(self.database_path)
        self.artifacts = ArtifactStore(self.ledger, [self.artifact_root])

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _machine_config(self, catalog_path: Path) -> Path:
        config_path = self.data_root / "machine.json"
        config_path.write_text(
            json.dumps(
                {
                    "configKind": "relay-liveloop-machine",
                    "schemaVersion": 1,
                    "dataRoot": str(self.data_root),
                    "playerProcess": {
                        "baselineCatalogPath": str(catalog_path),
                        "registrationDirectory": str(self.data_root / "player-process" / "registrations"),
                        "runtimeSessionFile": str(self.session_file),
                        "statePath": str(self.data_root / "player-process" / "state.json"),
                        "stopTimeoutSeconds": 5,
                    },
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def _approval(self, *, baseline_id: str | None) -> tuple[str, str]:
        impact = {
            "hotfix": False,
            "restartPlayer": True,
            "buildBaseline": False,
            "rebuildViews": [],
            "reloadModules": [],
        }
        task = self.ledger.create_task(
            {
                "sessionId": "session-composition-1",
                "goal": "synthetic player process composition test",
                "target": {"kind": "player"},
                "allowedImpact": impact,
                "acceptance": {"runtime": "not_run"},
            }
        )
        details = {"requiredImpact": impact}
        if baseline_id is not None:
            details["baselineId"] = baseline_id
        plan = self.ledger.create_plan(
            {
                "taskId": task["taskId"],
                "sessionId": task["sessionId"],
                "inputSnapshot": "synthetic-input",
                "expectedRuntimeRevision": "runtime-composition-1",
                "route": "player",
                "state": "prepared",
                "prepareComplete": True,
                "approvalRequired": True,
                "details": details,
            }
        )
        approval = self.ledger.approve_plan(task["taskId"], plan["planId"], "confirm-composition", impact)
        return approval["approvalId"], approval["userConfirmationRef"]

    def test_catalog_reuses_verified_ledger_artifact_and_session_metadata(self) -> None:
        executable = self.artifact_root / "synthetic-player.bin"
        executable.write_bytes(b"synthetic-player-baseline-bytes")
        artifact = self.artifacts.register(executable, kind="player_baseline")
        catalog = self.data_root / "player-baselines.json"
        catalog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "baselines": {
                        artifact["artifactId"]: {
                            "artifactId": artifact["artifactId"],
                            "executablePath": str(executable),
                            "arguments": ["--synthetic"],
                            "workingDirectory": str(self.root),
                            "environment": {"PLAYER_MODE": "synthetic"},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        config = composition_module.load_player_process_config(self._machine_config(catalog))
        metadata = composition_module.load_runtime_session_metadata(config.runtime_session_file)
        self.assertFalse(hasattr(metadata, "shared_secret"))
        resolver = composition_module.LedgerBaselineResolver(self.ledger, self.artifacts, config)
        resolver.preflight()
        spec = resolver.resolve(artifact["artifactId"])
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(str(executable.resolve()), spec.executable_path)
        self.assertEqual(("--synthetic",), spec.arguments)
        self.assertEqual(metadata.session_id, spec.session_id)
        self.assertEqual(metadata.launch_id, spec.launch_id)
        provider = composition_module.create_player_process_provider(
            config=config,
            ledger=self.ledger,
            artifacts=self.artifacts,
        )
        self.assertTrue(provider.is_verified)

    def test_authorization_reads_existing_approval_and_runtime_registry_is_exact(self) -> None:
        approval_id, confirmation_ref = self._approval(baseline_id="artifact-composition-1")
        verifier = composition_module.LedgerAuthorizationVerifier(self.ledger)
        verifier.preflight()
        by_approval = verifier.authorize(
            "player.start",
            approval_id,
            baseline_id="artifact-composition-1",
            session_id="session-composition-1",
        )
        by_confirmation = verifier.authorize("player.stop", confirmation_ref, session_id="session-composition-1")
        self.assertTrue(by_approval.allowed)
        self.assertTrue(by_confirmation.allowed)
        wrong_session = verifier.authorize("player.stop", confirmation_ref, session_id="session-other")
        self.assertFalse(wrong_session.allowed)
        unscoped_id, _ = self._approval(baseline_id=None)
        unscoped = verifier.authorize(
            "player.start",
            unscoped_id,
            baseline_id="artifact-composition-1",
            session_id="session-composition-1",
        )
        self.assertFalse(unscoped.allowed)

        metadata = composition_module.load_runtime_session_metadata(self.session_file)
        registry = composition_module.AuthenticatedRuntimeSessionRegistry(_Transport(), metadata)
        current = registry.current()
        self.assertIsNotNone(current)
        self.assertTrue(registry.matches("session-composition-1", "launch-composition-1"))
        self.assertFalse(registry.matches("session-other", "launch-composition-1"))
        self.assertFalse(bool(current and current.runtime_revision != metadata.runtime_revision))

    def test_native_stop_keeps_the_same_handle_until_post_stop_observation(self) -> None:
        events: list[str] = []

        class Kernel:
            def __init__(self) -> None:
                self.exit_code = composition_module.WindowsProcessAdapter._STILL_ACTIVE

            def GetExitCodeProcess(self, handle: Any, output: Any) -> bool:
                events.append("exit")
                output._obj.value = self.exit_code
                return True

            def TerminateProcess(self, handle: Any, code: int) -> bool:
                events.append("terminate")
                self.exit_code = 0
                return True

            def WaitForSingleObject(self, handle: Any, timeout_ms: int) -> int:
                events.append("wait")
                return composition_module.WindowsProcessAdapter._WAIT_OBJECT_0

            def CloseHandle(self, handle: Any) -> bool:
                events.append("close")
                return True

        class IdentityReader:
            def read_handle(self, handle: Any, process_id: int) -> Any:
                events.append("identity_same_handle")
                return provider_module.ProcessIdentity(process_id, "start", "C:\\Player.exe")

        adapter = composition_module.WindowsProcessAdapter.__new__(composition_module.WindowsProcessAdapter)
        adapter._kernel32 = Kernel()
        adapter._identity_reader = IdentityReader()
        adapter._registrations = None
        adapter._runtime_sessions = None
        adapter._owned = {}
        adapter._pending_sessions = {}
        adapter._lock = threading.RLock()
        adapter._session_for_process = lambda process_id: ("session-composition-1", "launch-composition-1")
        handle = composition_module.WindowsProcessHandle(77, native_handle=object())

        before = adapter.inspect(handle)
        self.assertEqual("running", before.status)
        self.assertIn("identity_same_handle", events)
        stopped = adapter.stop(handle, 1)
        self.assertEqual("stopped", stopped.status)
        self.assertIsNotNone(handle.native_handle)
        self.assertNotIn("close", events)
        after = adapter.inspect(handle)
        self.assertEqual("not_found", after.status)
        adapter.release(handle)
        self.assertIsNone(handle.native_handle)
        self.assertEqual(1, events.count("close"))


if __name__ == "__main__":
    unittest.main()
