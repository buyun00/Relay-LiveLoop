from __future__ import annotations

import importlib.util
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


configured_public_root = os.environ.get("RELAY_LIVELOOP_SUBJECT")
if not configured_public_root:
    raise RuntimeError("Set RELAY_LIVELOOP_SUBJECT to the public repository root for focused provider tests.")
PUBLIC_ROOT = Path(configured_public_root).resolve()
MODULE_PATH = PUBLIC_ROOT / "host" / "player_process_provider.py"
if str(PUBLIC_ROOT) not in sys.path:
    sys.path.insert(0, str(PUBLIC_ROOT))
spec = importlib.util.spec_from_file_location("isolated_player_process_provider", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Resolver:
    def __init__(self, launch_spec: Any) -> None:
        self.launch_spec = launch_spec
        self.calls = 0

    def resolve(self, baseline_id: str) -> Any:
        self.calls += 1
        return self.launch_spec if baseline_id == self.launch_spec.baseline_id else None


class Authorization:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def authorize(self, operation: str, authorization_ref: str, *, baseline_id=None, session_id=None):
        self.calls.append((operation, authorization_ref, baseline_id, session_id))
        return module.AuthorizationDecision(authorization_ref in {"allow-start", "allow-stop"})


class FakeHandle:
    def __init__(self, process_id: int) -> None:
        self.pid = process_id


class FakeAdapter:
    def __init__(self, identity: Any, session_id: str, launch_id: str) -> None:
        self.identity = identity
        self.session_id = session_id
        self.launch_id = launch_id
        self.handle = FakeHandle(identity.process_id)
        self.running = False
        self.locate_status = "not_found"
        self.inspect_identity = identity
        self.inspect_session_id = session_id
        self.inspect_launch_id = launch_id
        self.start_mode = "success"
        self.stop_mode = "success"
        self.start_calls: list[tuple[str, tuple[str, ...], str, dict[str, str]]] = []
        self.attach_calls: list[int] = []
        self.stop_calls = 0

    def locate(self, session_id: str):
        if self.locate_status == "unknown":
            return module.ProcessObservation.unknown("synthetic_lookup_unknown")
        if self.locate_status == "not_found":
            return module.ProcessObservation.not_found()
        return module.ProcessObservation.running(
            self.handle,
            self.inspect_identity,
            session_id=self.inspect_session_id,
            launch_id=self.inspect_launch_id,
        )

    def attach(self, process_id: int):
        self.attach_calls.append(process_id)
        return self.handle if process_id == self.identity.process_id else None

    def start(self, executable_path, arguments, working_directory, environment):
        self.start_calls.append((executable_path, tuple(arguments), working_directory, dict(environment)))
        if self.start_mode == "raise":
            raise RuntimeError("synthetic start outcome unknown")
        self.running = True
        return self.handle

    def inspect(self, process):
        if not self.running:
            return module.ProcessObservation.not_found("synthetic_process_exited")
        return module.ProcessObservation.running(
            process,
            self.inspect_identity,
            session_id=self.inspect_session_id,
            launch_id=self.inspect_launch_id,
        )

    def stop(self, process, timeout_seconds: float):
        self.stop_calls += 1
        if self.stop_mode == "unknown":
            return module.ProcessStopResult("unknown", reason="synthetic_stop_unknown")
        if self.stop_mode == "still_running":
            return module.ProcessStopResult("still_running", reason="synthetic_stop_timeout")
        self.running = False
        return module.ProcessStopResult("stopped")


class RegistrationFailAdapter(FakeAdapter):
    def __init__(self, identity: Any, session_id: str, launch_id: str) -> None:
        super().__init__(identity, session_id, launch_id)
        self.release_calls = 0
        self.forget_calls = 0

    def record_owned_process(self, observation: Any) -> None:
        raise OSError("synthetic registration write failure")

    def forget_owned_process(self, record: Any) -> None:
        self.forget_calls += 1

    def release(self, process: Any) -> None:
        self.release_calls += 1


class FailingPrimaryStateStore:
    def __init__(self) -> None:
        self.state = module.PlayerState()
        self.write_calls = 0
        self.marker_calls = 0

    def read(self) -> Any:
        return copy.deepcopy(self.state)

    def write(self, state: Any) -> None:
        self.write_calls += 1
        raise OSError("synthetic primary state write failure")

    def write_unresolved_marker(self, state: Any) -> None:
        self.marker_calls += 1
        self.state = copy.deepcopy(state)


class FailingAllStateStore(FailingPrimaryStateStore):
    def write_unresolved_marker(self, state: Any) -> None:
        self.marker_calls += 1
        raise OSError("synthetic unresolved marker write failure")


class FailingAfterIntentStateStore(FailingPrimaryStateStore):
    def write(self, state: Any) -> None:
        self.write_calls += 1
        if self.write_calls == 1:
            self.state = copy.deepcopy(state)
            return
        raise OSError("synthetic post-intent state write failure")


class PlayerProcessProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="player-process-provider-")
        self.root = Path(self.temporary.name)
        self.executable = self.root / "synthetic-player.bin"
        self.executable.write_bytes(b"synthetic executable placeholder")
        self.session_file = self.root / "session-handoff.json"
        self.session_file.write_text(json.dumps({"opaque": "not-read-by-provider"}), encoding="utf-8")
        self.session_id = "session-synthetic-1"
        self.launch_id = "launch-synthetic-1"
        self.baseline_id = "baseline-synthetic-1"
        self.identity = module.ProcessIdentity(4101, "2026-09-16T00:00:00.000000Z", str(self.executable))
        self.launch_spec = module.PlayerLaunchSpec(
            baseline_id=self.baseline_id,
            session_id=self.session_id,
            launch_id=self.launch_id,
            executable_path=str(self.executable),
            arguments=("--mode", "synthetic"),
            working_directory=str(self.root),
            session_file_path=str(self.session_file),
            environment={"PLAYER_MODE": "synthetic"},
        )
        self.resolver = Resolver(self.launch_spec)
        self.authorization = Authorization()
        self.adapter = FakeAdapter(self.identity, self.session_id, self.launch_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provider(self, *, state_store=None, adapter=None):
        return module.PlayerProcessProvider(
            self.resolver,
            adapter or self.adapter,
            authorization=self.authorization,
            state_store=state_store,
            verified=True,
        )

    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.provider_instance.execute(operation, {"requestId": "request-synthetic", "arguments": arguments})

    def test_start_uses_explicit_environment_and_session_file_env_only(self) -> None:
        self.provider_instance = self.provider()
        response = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("completed", response["status"])
        self.assertTrue(response["runtimeChanged"])
        self.assertEqual(1, len(self.adapter.start_calls))
        _executable, arguments, _cwd, environment = self.adapter.start_calls[0]
        self.assertEqual(("--mode", "synthetic"), arguments)
        self.assertEqual(
            {
                "PLAYER_MODE": "synthetic",
                module.SESSION_FILE_ENV: str(self.session_file),
            },
            environment,
        )
        self.assertNotIn(str(self.session_file), arguments)
        self.assertNotIn("not-read-by-provider", json.dumps(response))
        self.assertTrue(response["result"]["session"]["identityVerified"])
        self.assertTrue(response["result"]["session"]["ownedByProvider"])
        self.assertEqual(
            [("player.start", "allow-start", self.baseline_id, self.session_id)],
            self.authorization.calls,
        )

    def test_attached_process_is_preserved_and_stop_is_refused(self) -> None:
        self.adapter.locate_status = "running"
        self.provider_instance = self.provider()
        started = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("completed", started["status"])
        self.assertEqual("attached_preserved", started["result"]["session"]["state"])
        self.assertFalse(started["result"]["session"]["ownedByProvider"])
        self.assertEqual([], self.adapter.start_calls)

        stopped = self.command(
            "player.stop",
            {"sessionId": self.session_id, "authorizationRef": "allow-stop"},
        )
        self.assertEqual("failed", stopped["status"])
        self.assertEqual("CONFLICT", stopped["error"]["code"])
        self.assertFalse(stopped["runtimeChanged"])
        self.assertEqual(0, self.adapter.stop_calls)

    def test_process_mutation_requires_explicit_authorization(self) -> None:
        self.provider_instance = self.provider()
        start = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "not-authorized"},
        )
        self.assertEqual("failed", start["status"])
        self.assertEqual("AUTH_REQUIRED", start["error"]["code"])
        self.assertEqual([], self.adapter.start_calls)

        stop = self.command(
            "player.stop",
            {"sessionId": self.session_id, "authorizationRef": "not-authorized"},
        )
        self.assertEqual("failed", stop["status"])
        self.assertEqual("AUTH_REQUIRED", stop["error"]["code"])
        self.assertEqual(0, self.adapter.stop_calls)

    def test_unknown_existing_process_lookup_does_not_start_a_duplicate(self) -> None:
        self.adapter.locate_status = "unknown"
        self.provider_instance = self.provider()
        response = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", response["status"])
        self.assertIsNone(response["runtimeChanged"])
        self.assertEqual([], self.adapter.start_calls)

    def test_unknown_start_blocks_automatic_retry(self) -> None:
        self.adapter.start_mode = "raise"
        self.provider_instance = self.provider()
        first = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        second = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", first["status"])
        self.assertIsNone(first["runtimeChanged"])
        self.assertEqual("state_unknown", second["status"])
        self.assertIsNone(second["runtimeChanged"])
        self.assertEqual("STATE_UNKNOWN", second["error"]["code"])
        self.assertEqual(1, len(self.adapter.start_calls))
        self.assertEqual(1, self.resolver.calls)

    def test_registration_failure_cleans_the_same_owned_handle_and_blocks_retry(self) -> None:
        adapter = RegistrationFailAdapter(self.identity, self.session_id, self.launch_id)
        self.provider_instance = self.provider(adapter=adapter)
        first = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        second = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", first["status"])
        self.assertEqual("state_unknown", second["status"])
        self.assertEqual(1, len(adapter.start_calls))
        self.assertEqual(1, adapter.stop_calls)
        self.assertEqual(1, adapter.release_calls)
        self.assertFalse(adapter.running)

    def test_primary_state_failure_writes_unresolved_marker_and_blocks_retry(self) -> None:
        adapter = RegistrationFailAdapter(self.identity, self.session_id, self.launch_id)
        # Replace the registration failure with a successful registration hook;
        # the primary state store is the failing boundary under test.
        adapter.record_owned_process = lambda observation: None
        store = FailingAfterIntentStateStore()
        self.provider_instance = self.provider(state_store=store, adapter=adapter)
        first = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        second = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", first["status"])
        self.assertEqual("state_unknown", second["status"])
        self.assertEqual("STATE_UNKNOWN", second["error"]["code"])
        self.assertEqual(1, len(adapter.start_calls))
        self.assertEqual(1, adapter.stop_calls)
        self.assertEqual(1, adapter.release_calls)
        self.assertEqual(1, store.marker_calls)
        self.assertFalse(adapter.running)

    def test_unavailable_state_store_keeps_an_inflight_block_without_fake_durability(self) -> None:
        adapter = RegistrationFailAdapter(self.identity, self.session_id, self.launch_id)
        adapter.record_owned_process = lambda observation: None
        store = FailingAllStateStore()
        self.provider_instance = self.provider(state_store=store, adapter=adapter)
        first = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        second = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", first["status"])
        self.assertEqual("state_unknown", second["status"])
        self.assertEqual(0, len(adapter.start_calls))
        self.assertEqual(0, adapter.stop_calls)
        self.assertEqual(0, adapter.release_calls)
        self.assertEqual(1, store.marker_calls)
        self.assertFalse(adapter.running)

    def test_durable_start_intent_blocks_a_new_provider_after_unknown_cleanup(self) -> None:
        state_path = self.root / "state" / "shared.json"
        first_store = module.JsonPlayerStateStore(state_path)
        first_adapter = RegistrationFailAdapter(self.identity, self.session_id, self.launch_id)
        first_adapter.stop_mode = "unknown"
        self.provider_instance = self.provider(state_store=first_store, adapter=first_adapter)
        first = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("state_unknown", first["status"])
        self.assertEqual(1, len(first_adapter.start_calls))
        self.assertEqual(1, first_adapter.stop_calls)
        self.assertEqual(1, first_adapter.release_calls)
        self.assertEqual(0, first_adapter.forget_calls)
        self.assertIn(self.baseline_id, first_store.read().start_intents)

        second_adapter = RegistrationFailAdapter(self.identity, self.session_id, self.launch_id)
        second = self.provider(state_store=module.JsonPlayerStateStore(state_path), adapter=second_adapter).execute(
            "player.start",
            {"requestId": "request-recreated-provider", "arguments": {"baselineId": self.baseline_id, "authorizationRef": "allow-start"}},
        )
        self.assertEqual("state_unknown", second["status"])
        self.assertEqual(0, len(second_adapter.start_calls))
        self.assertEqual(0, second_adapter.stop_calls)

        reconcile_adapter = FakeAdapter(self.identity, self.session_id, self.launch_id)
        reconcile_adapter.running = True
        reconciled = self.provider(state_store=module.JsonPlayerStateStore(state_path), adapter=reconcile_adapter).execute(
            "player.attach",
            {"requestId": "request-reconcile", "arguments": {"sessionId": self.session_id}},
        )
        self.assertEqual("completed", reconciled["status"])
        self.assertEqual("reattached", reconciled["result"]["session"]["state"])
        self.assertNotIn(self.baseline_id, module.JsonPlayerStateStore(state_path).read().start_intents)

    def test_attach_rebinds_only_when_pid_start_launch_and_path_match(self) -> None:
        state_store = module.InMemoryPlayerStateStore()
        self.provider_instance = self.provider(state_store=state_store)
        started = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("completed", started["status"])

        rebound_adapter = FakeAdapter(self.identity, self.session_id, self.launch_id)
        rebound_adapter.running = True
        rebound = self.provider(state_store=state_store, adapter=rebound_adapter)
        attached = rebound.execute("player.attach", {"requestId": "request-attach", "arguments": {"sessionId": self.session_id}})
        self.assertEqual("completed", attached["status"])
        self.assertEqual("reattached", attached["result"]["session"]["state"])
        self.assertTrue(attached["result"]["session"]["ownedByProvider"])
        self.assertEqual([self.identity.process_id], rebound_adapter.attach_calls)

        stopped = rebound.execute(
            "player.stop",
            {"requestId": "request-stop", "arguments": {"sessionId": self.session_id, "authorizationRef": "allow-stop"}},
        )
        self.assertEqual("completed", stopped["status"])
        self.assertEqual("stopped", stopped["result"]["session"]["state"])
        self.assertTrue(stopped["runtimeChanged"])
        self.assertEqual(1, rebound_adapter.stop_calls)

    def test_pid_reuse_or_launch_mismatch_refuses_stop_and_blocks_retry(self) -> None:
        state_store = module.InMemoryPlayerStateStore()
        self.provider_instance = self.provider(state_store=state_store)
        started = self.command(
            "player.start",
            {"baselineId": self.baseline_id, "authorizationRef": "allow-start"},
        )
        self.assertEqual("completed", started["status"])
        self.adapter.inspect_identity = module.ProcessIdentity(9999, self.identity.start_time_utc, str(self.executable))

        stopped = self.command(
            "player.stop",
            {"sessionId": self.session_id, "authorizationRef": "allow-stop"},
        )
        self.assertEqual("state_unknown", stopped["status"])
        self.assertIsNone(stopped["runtimeChanged"])
        self.assertEqual(0, self.adapter.stop_calls)
        self.assertEqual("unknown", state_store.read().sessions[self.session_id].state)

        retry = self.command(
            "player.stop",
            {"sessionId": self.session_id, "authorizationRef": "allow-stop"},
        )
        self.assertEqual("state_unknown", retry["status"])
        self.assertEqual(0, self.adapter.stop_calls)

    def test_json_state_store_round_trip_has_no_process_handle_or_secret(self) -> None:
        path = self.root / "state" / "player.json"
        store = module.JsonPlayerStateStore(path)
        record = module.PlayerSessionRecord(
            session_id=self.session_id,
            launch_id=self.launch_id,
            baseline_id=self.baseline_id,
            identity=self.identity,
            expected_executable_path=str(self.executable),
            owned_by_provider=True,
            ownership_id="owned_synthetic",
            state="started",
            stop_allowed=True,
            created_at_utc="2026-09-16T00:00:00Z",
            updated_at_utc="2026-09-16T00:00:00Z",
        )
        store.write(module.PlayerState(sessions={self.session_id: record}))
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("not-read-by-provider", raw)
        self.assertNotIn("handle", raw.lower())
        loaded = store.read()
        self.assertEqual(self.identity, loaded.sessions[self.session_id].identity)

    def test_json_state_store_reads_and_clears_the_unresolved_marker(self) -> None:
        path = self.root / "state" / "marker.json"
        store = module.JsonPlayerStateStore(path)
        unresolved = module.PlayerState(blocked_baselines={self.baseline_id: "synthetic_unresolved"})
        store.write_unresolved_marker(unresolved)
        self.assertEqual("synthetic_unresolved", store.read().blocked_baselines[self.baseline_id])
        store.write(module.PlayerState())
        self.assertNotIn(self.baseline_id, store.read().blocked_baselines)


if __name__ == "__main__":
    unittest.main()
