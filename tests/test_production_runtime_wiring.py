from __future__ import annotations

import json

import unittest

from host.runtime_session import load_runtime_session
from host.runtime_transport_provider import RuntimeTransportProvider


class ProductionRuntimeWiringTests(unittest.TestCase):
    def test_runtime_session_loader_rejects_missing_secret(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as root:
            path = Path(root) / "runtime-session.json"
            path.write_text(json.dumps({
                "sessionId": "session-1",
                "launchId": "launch-1",
                "runtimeRevision": "rev-1",
                "protocolVersion": 1,
                "sharedSecretBase64": "not-a-secret",
                "hostAddress": "127.0.0.1",
                "port": 18761,
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "sharedSecretBase64"):
                load_runtime_session(path)


    def test_runtime_provider_is_unavailable_until_authenticated(self):
        class Transport:
            authenticated = False

        provider = RuntimeTransportProvider(Transport(), {"observe"})
        self.assertFalse(provider.is_verified)


    def test_runtime_provider_allowlist_rejects_unowned_operation(self):
        class Transport:
            authenticated = True

            def execute(self, operation, command):
                return {"operation": operation}

        provider = RuntimeTransportProvider(Transport(), {"observe"})
        with self.assertRaisesRegex(ValueError, "not supported"):
            provider.execute("verify", {"requestId": "request-1"})

    def test_input_mutation_operations_are_protocol_bound_to_verification(self):
        from host.service import PROVIDER_OPERATIONS
        from host.validation import validate_command

        self.assertEqual(PROVIDER_OPERATIONS["input.click"], "verification")
        self.assertEqual(PROVIDER_OPERATIONS["input.text"], "verification")
        click = validate_command({
            "protocolVersion": 1, "requestId": "request-click", "taskId": "task-1",
            "operation": "input.click", "arguments": {
                "targetId": "weekly-card-page", "expectedOwnerGeneration": 0,
                "expectedFrame": 1, "expectedViewportGeneration": 0,
                "screenX": 10.0, "screenY": 20.0,
            },
        })
        self.assertEqual(click["operation"], "input.click")


if __name__ == "__main__":
    unittest.main()
