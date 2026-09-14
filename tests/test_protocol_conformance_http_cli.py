"""Black-box Relay LiveLoop protocol conformance tests.

The suite launches the real HTTP host, CLI, and MCP stdio adapter as
subprocesses.  Set RELAY_LIVELOOP_SUBJECT to the foundation root and optionally
RELAY_LIVELOOP_OVERLAY to an overlay root when the test file is outside the
subject; after integration, the repository root is inferred for both.

All identifiers, content, paths created by the tests, and bearer tokens are
synthetic.  Ledger seeding is used only to arrange neutral artifact and job
records whose provider-producing workflows are not part of the foundation
delivery.  It does not stand in for a provider or runtime success.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any


FACT_KEYS = {
    "sourceSaved",
    "runtimeMatched",
    "checksPassed",
    "visualReviewed",
    "freshVerified",
}
RESULT_STATUSES = {
    "accepted",
    "completed",
    "approval_required",
    "failed",
    "state_unknown",
}
ERROR_CODES = {
    "CAPABILITY_UNAVAILABLE",
    "AUTH_REQUIRED",
    "CONTRACT_MISMATCH",
    "WRONG_SESSION",
    "STALE_TARGET",
    "INPUT_CHANGED",
    "COMPILE_FAILED",
    "RESOURCE_BUILD_FAILED",
    "APPROVAL_REQUIRED",
    "UNLOAD_REFUSED",
    "RESTORE_FAILED",
    "STATE_UNKNOWN",
    "INVALID_REQUEST",
    "NOT_FOUND",
    "CONFLICT",
    "INTERNAL_ERROR",
}
RESULT_REQUIRED_KEYS = {
    "requestId",
    "status",
    "runtimeChanged",
    "facts",
    "error",
    "artifacts",
}
RESULT_ALLOWED_KEYS = RESULT_REQUIRED_KEYS | {
    "jobId",
    "planId",
    "result",
    "timingsMs",
}


def inferred_subject_root() -> Path:
    configured = os.environ.get("RELAY_LIVELOOP_SUBJECT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


def modern_mcp_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "synthetic-conformance-client",
            "version": "1.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def assert_result_envelope(
    case: unittest.TestCase,
    value: Any,
    *,
    expected_request_id: str | None = None,
) -> None:
    """Apply the public v1 result-schema constraints without extra packages."""
    case.assertIsInstance(value, dict)
    keys = set(value)
    case.assertFalse(RESULT_REQUIRED_KEYS - keys, f"missing result keys: {RESULT_REQUIRED_KEYS - keys}")
    case.assertFalse(keys - RESULT_ALLOWED_KEYS, f"unexpected result keys: {keys - RESULT_ALLOWED_KEYS}")

    request_id = value["requestId"]
    case.assertIsInstance(request_id, str)
    case.assertGreaterEqual(len(request_id), 1, "result requestId must be non-empty")
    if expected_request_id is not None:
        case.assertEqual(expected_request_id, request_id)

    case.assertIn(value["status"], RESULT_STATUSES)
    case.assertTrue(value["runtimeChanged"] is None or type(value["runtimeChanged"]) is bool)

    facts = value["facts"]
    case.assertIsInstance(facts, dict)
    case.assertEqual(FACT_KEYS, set(facts))
    for fact in facts.values():
        case.assertTrue(fact is None or type(fact) is bool)

    error = value["error"]
    if error is not None:
        case.assertIsInstance(error, dict)
        case.assertEqual({"code", "stage", "message", "recoverable", "details"}, set(error))
        case.assertIn(error["code"], ERROR_CODES)
        case.assertIsInstance(error["stage"], str)
        case.assertTrue(error["stage"])
        case.assertIsInstance(error["message"], str)
        case.assertTrue(error["message"])
        case.assertIs(type(error["recoverable"]), bool)
        case.assertIsInstance(error["details"], dict)

    case.assertIsInstance(value["artifacts"], list)
    for artifact in value["artifacts"]:
        case.assertIsInstance(artifact, dict)
        case.assertFalse(set(artifact) - {"artifactId", "kind", "sha256", "mediaType", "size"})
        case.assertTrue({"artifactId", "kind", "sha256"} <= set(artifact))
        case.assertRegex(artifact["sha256"], r"^[0-9a-fA-F]{64}$")

    if "result" in value:
        case.assertIsInstance(value["result"], dict)
    for identifier_key in ("jobId", "planId"):
        if identifier_key in value:
            case.assertTrue(value[identifier_key] is None or isinstance(value[identifier_key], str))
    if "timingsMs" in value:
        case.assertIsInstance(value["timingsMs"], dict)
        for timing in value["timingsMs"].values():
            case.assertTrue(type(timing) in (int, float) and timing >= 0)


def allowed_impact() -> dict[str, Any]:
    return {
        "hotfix": True,
        "rebuildViews": ["synthetic-panel"],
        "reloadModules": [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


def task_arguments(goal: str = "Adjust a synthetic panel.") -> dict[str, Any]:
    return {
        "goal": goal,
        "sessionId": "synthetic-session",
        "target": "synthetic-panel/primary-control",
        "reference": "synthetic-panel/reference-control",
        "allowedImpact": allowed_impact(),
        "acceptance": ["The synthetic alignment assertion is satisfied."],
    }


def envelope(
    operation: str,
    request_id: str,
    arguments: dict[str, Any],
    task_id: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "protocolVersion": 1,
        "requestId": request_id,
        "operation": operation,
        "arguments": arguments,
    }
    if task_id is not None:
        value["taskId"] = task_id
    return value


class HTTPCLIProtocolConformance(unittest.TestCase):
    token = "synthetic-local-bearer-token"

    @classmethod
    def setUpClass(cls) -> None:
        cls.subject_root = inferred_subject_root()
        overlay_value = os.environ.get("RELAY_LIVELOOP_OVERLAY")
        cls.overlay_root = Path(overlay_value).resolve() if overlay_value else cls.subject_root
        contract_value = os.environ.get("RELAY_LIVELOOP_CONTRACT_ROOT")
        cls.contract_root = Path(contract_value).resolve() if contract_value else cls.subject_root / "contracts"
        if not (cls.subject_root / "relay_liveloop.py").is_file():
            raise RuntimeError(
                "Relay LiveLoop subject not found. Set RELAY_LIVELOOP_SUBJECT to its repository root."
            )
        if not (cls.overlay_root / "mcp" / "stdio_server.py").is_file():
            raise RuntimeError(
                "Relay LiveLoop MCP overlay not found. Set RELAY_LIVELOOP_OVERLAY when testing an overlay delivery."
            )
        for contract_name in ("operations.json", "result.schema.json"):
            if not (cls.contract_root / contract_name).is_file():
                raise RuntimeError(
                    "Relay LiveLoop public contracts not found. Set RELAY_LIVELOOP_CONTRACT_ROOT."
                )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-liveloop-protocol-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "state" / "host.sqlite3"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(parents=True)
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()
        self.temporary.cleanup()

    @staticmethod
    def _creationflags() -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    @staticmethod
    def _ephemeral_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port in {18760, 18761}:
            return HTTPCLIProtocolConformance._ephemeral_port()
        return port

    def subprocess_environment(self, *, include_token: bool = True) -> dict[str, str]:
        environment = os.environ.copy()
        roots = [str(self.overlay_root)]
        if self.subject_root != self.overlay_root:
            roots.append(str(self.subject_root))
        existing_pythonpath = environment.get("PYTHONPATH")
        if existing_pythonpath:
            roots.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(roots)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["RELAY_LIVELOOP_CONTRACT_ROOT"] = str(self.contract_root)
        if include_token:
            environment["RELAY_LIVELOOP_TOKEN"] = self.token
        else:
            environment.pop("RELAY_LIVELOOP_TOKEN", None)
        return environment

    def start_server(self) -> None:
        self.assertIsNone(self.process)
        self.port = self._ephemeral_port()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "relay_liveloop",
                "serve",
                "--database",
                str(self.database),
                "--artifact-root",
                str(self.artifact_root),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=self.root,
            env=self.subprocess_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=self._creationflags(),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(f"Host exited during startup. stdout={stdout!r} stderr={stderr!r}")
            try:
                status, _headers, raw = self.http_request("GET", "/status")
                if status == 200 and json.loads(raw.decode("utf-8"))["service"] == "Relay LiveLoop":
                    return
            except (ConnectionError, OSError, json.JSONDecodeError, KeyError):
                pass
            time.sleep(0.05)
        self.fail("Host did not become ready on its ephemeral loopback port.")

    def stop_server(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def http_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        token: str | None = token,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        assert self.port is not None
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, raw
        finally:
            connection.close()

    def json_request(
        self,
        method: str,
        path: str,
        *,
        value: Any | None = None,
        token: str | None = token,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        body = raw_body
        if value is not None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            content_type = content_type or "application/json"
        status, headers, raw = self.http_request(
            method,
            path,
            body=body,
            token=token,
            content_type=content_type,
        )
        self.assertIn("application/json", headers.get("content-type", ""))
        return status, headers, json.loads(raw.decode("utf-8"))

    def command(self, value: Any, *, token: str | None = token) -> tuple[int, dict[str, Any]]:
        status, _headers, result = self.json_request("POST", "/commands", value=value, token=token)
        return status, result

    def run_cli(self, *arguments: str, include_token: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "relay_liveloop", *arguments],
            cwd=self.root,
            env=self.subprocess_environment(include_token=include_token),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=self._creationflags(),
        )

    def run_mcp(self, messages: list[Any]) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
        wire = "".join(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            for message in messages
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(self.overlay_root / "mcp" / "stdio_server.py"),
                "--url",
                self.base_url,
                "--operations",
                str(self.contract_root / "operations.json"),
                "--result-schema",
                str(self.contract_root / "result.schema.json"),
            ],
            cwd=self.root,
            env=self.subprocess_environment(),
            input=wire,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=self._creationflags(),
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line]
        return completed, responses

    def open_task(self, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        request = envelope("task.open", request_id, task_arguments())
        status, response = self.command(request)
        self.assertEqual(200, status)
        assert_result_envelope(self, response, expected_request_id=request_id)
        self.assertEqual("completed", response["status"])
        return request, response

    def _import_subject(self, module: str) -> Any:
        roots = [str(self.overlay_root)]
        if self.subject_root != self.overlay_root:
            roots.append(str(self.subject_root))
        for root in reversed(roots):
            if root not in sys.path:
                sys.path.insert(0, root)
        importlib.invalidate_caches()
        return importlib.import_module(module)

    def seed_artifact(self, path: Path) -> dict[str, Any]:
        self.stop_server()
        ledger_module = self._import_subject("host.ledger")
        artifacts_module = self._import_subject("host.artifacts")
        ledger = ledger_module.Ledger(self.database)
        try:
            store = artifacts_module.ArtifactStore(ledger, [self.artifact_root])
            metadata = store.register(path, kind="synthetic-evidence")
        finally:
            ledger.close()
        self.start_server()
        return metadata

    def seed_jobs(self) -> None:
        self.stop_server()
        ledger_module = self._import_subject("host.ledger")
        ledger = ledger_module.Ledger(self.database)
        try:
            records = [
                {
                    "jobId": "job-synthetic-cancellable",
                    "requestId": "origin-synthetic-cancellable",
                    "operation": "prepare",
                    "state": "queued",
                    "stage": "prepare",
                    "runtimeChanged": False,
                },
                {
                    "jobId": "job-synthetic-applying",
                    "requestId": "origin-synthetic-applying",
                    "operation": "iterate",
                    "state": "running",
                    "stage": "runtime_apply",
                    "runtimeChanged": False,
                },
                {
                    "jobId": "job-synthetic-terminal",
                    "requestId": "origin-synthetic-terminal",
                    "operation": "verify",
                    "state": "completed",
                    "stage": "complete",
                    "runtimeChanged": False,
                    "result": {"summary": "synthetic terminal result"},
                },
            ]
            for record in records:
                ledger.create_job(record)
        finally:
            ledger.close()
        self.start_server()

    def test_authenticated_http_and_cli_commands_use_v1_envelopes_and_request_ids(self) -> None:
        request_id = "request-synthetic-http-status"
        status, response = self.command(envelope("status", request_id, {}))
        self.assertEqual(200, status)
        assert_result_envelope(self, response, expected_request_id=request_id)
        self.assertEqual("completed", response["status"])
        self.assertEqual("Relay LiveLoop", response["result"]["service"])
        self.assertEqual(1, response["result"]["protocolVersion"])

        first_cli = self.run_cli("status", "--url", self.base_url, "--json")
        second_cli = self.run_cli("status", "--url", self.base_url, "--json")
        self.assertEqual(0, first_cli.returncode, first_cli.stderr)
        self.assertEqual(0, second_cli.returncode, second_cli.stderr)
        first_result = json.loads(first_cli.stdout)
        second_result = json.loads(second_cli.stdout)
        assert_result_envelope(self, first_result)
        assert_result_envelope(self, second_result)
        self.assertRegex(first_result["requestId"], r"^request_[0-9a-f]{32}$")
        self.assertNotEqual(first_result["requestId"], second_result["requestId"])

    @property
    def base_url(self) -> str:
        assert self.port is not None
        return f"http://127.0.0.1:{self.port}"

    def test_authentication_rejection_is_a_v1_result_envelope(self) -> None:
        status, _headers, response = self.json_request("GET", "/status", token=None)
        self.assertEqual(401, status)
        self.assertEqual("failed", response["status"])
        self.assertEqual("AUTH_REQUIRED", response["error"]["code"])
        assert_result_envelope(self, response)

    def test_malformed_command_matrix_is_rejected_without_task_side_effects(self) -> None:
        valid_task = task_arguments()
        malformed_commands: list[tuple[str, Any]] = [
            ("non-object", []),
            (
                "unknown-envelope-field",
                {**envelope("status", "request-malformed-extra", {}), "syntheticExtra": True},
            ),
            (
                "wrong-protocol",
                {**envelope("status", "request-malformed-version", {}), "protocolVersion": 2},
            ),
            (
                "unsupported-operation",
                envelope("synthetic.unsupported", "request-malformed-operation", {}),
            ),
            (
                "missing-operation-arguments",
                envelope("task.open", "request-malformed-task", {"goal": "incomplete"}),
            ),
            (
                "conflicting-task-identifiers",
                envelope(
                    "task.show",
                    "request-malformed-task-conflict",
                    {"taskId": "task-synthetic-inner"},
                    "task-synthetic-outer",
                ),
            ),
            (
                "unknown-context-field",
                {
                    **envelope("status", "request-malformed-context", {}),
                    "context": {"syntheticUnknown": "value"},
                },
            ),
            (
                "wrong-impact-boolean-type",
                envelope(
                    "task.open",
                    "request-malformed-impact",
                    {
                        **valid_task,
                        "allowedImpact": {**allowed_impact(), "hotfix": 1},
                    },
                ),
            ),
        ]
        for label, malformed in malformed_commands:
            with self.subTest(label=label):
                status, response = self.command(malformed)
                self.assertEqual(400, status)
                assert_result_envelope(self, response)
                self.assertEqual("failed", response["status"])
                self.assertEqual("CONTRACT_MISMATCH", response["error"]["code"])
                self.assertFalse(response["runtimeChanged"])

        status, summary = self.command(envelope("status", "request-after-malformed-matrix", {}))
        self.assertEqual(200, status)
        self.assertEqual(0, summary["result"]["ledger"]["counts"]["tasks"])

    def test_task_state_continues_across_http_and_cli_updates(self) -> None:
        _request, opened = self.open_task("request-synthetic-task-open")
        task = opened["result"]["task"]
        task_id = task["taskId"]
        created_at = task["createdAt"]
        self.assertTrue(all(value is None for value in task["facts"].values()))

        shown = self.run_cli(
            "task.show",
            "--task",
            task_id,
            "--url",
            self.base_url,
            "--request-id",
            "request-synthetic-cli-show",
            "--json",
        )
        self.assertEqual(0, shown.returncode, shown.stderr)
        shown_result = json.loads(shown.stdout)
        assert_result_envelope(self, shown_result, expected_request_id="request-synthetic-cli-show")
        self.assertEqual(task_id, shown_result["result"]["task"]["taskId"])
        self.assertEqual(created_at, shown_result["result"]["task"]["createdAt"])

        replacement_goal = "Adjust the synthetic panel and preserve its neutral state."
        updated = self.run_cli(
            "task.update",
            "--task",
            task_id,
            "--arguments",
            json.dumps({"updates": {"goal": replacement_goal}}, separators=(",", ":")),
            "--url",
            self.base_url,
            "--request-id",
            "request-synthetic-cli-update",
            "--json",
        )
        self.assertEqual(0, updated.returncode, updated.stderr)
        updated_result = json.loads(updated.stdout)
        assert_result_envelope(self, updated_result, expected_request_id="request-synthetic-cli-update")
        self.assertEqual(replacement_goal, updated_result["result"]["task"]["goal"])

        status, final = self.command(
            envelope("task.show", "request-synthetic-http-show", {"taskId": task_id})
        )
        self.assertEqual(200, status)
        assert_result_envelope(self, final, expected_request_id="request-synthetic-http-show")
        self.assertEqual(replacement_goal, final["result"]["task"]["goal"])
        self.assertEqual(created_at, final["result"]["task"]["createdAt"])
        self.assertTrue(all(value is None for value in final["facts"].values()))

    def test_duplicate_request_replays_after_restart_and_conflict_is_refused(self) -> None:
        request, first = self.open_task("request-synthetic-restart-idempotency")
        task_id = first["result"]["taskId"]
        self.stop_server()
        self.start_server()

        status, replay = self.command(request)
        self.assertEqual(200, status)
        self.assertEqual(first, replay)

        conflicting_request = envelope(
            "task.open",
            request["requestId"],
            task_arguments("A conflicting synthetic goal."),
        )
        conflict_status, conflict = self.command(conflicting_request)
        self.assertEqual(400, conflict_status)
        assert_result_envelope(self, conflict, expected_request_id=request["requestId"])
        self.assertEqual("failed", conflict["status"])
        self.assertIn(conflict["error"]["code"], {"CONTRACT_MISMATCH", "CONFLICT"})
        self.assertEqual("idempotency", conflict["error"]["stage"])
        self.assertFalse(conflict["runtimeChanged"])

        summary_status, summary = self.command(envelope("status", "request-synthetic-count", {}))
        self.assertEqual(200, summary_status)
        self.assertEqual(1, summary["result"]["ledger"]["counts"]["tasks"])
        show_status, shown = self.command(
            envelope("task.show", "request-synthetic-restart-show", {"taskId": task_id})
        )
        self.assertEqual(200, show_status)
        self.assertEqual(task_id, shown["result"]["task"]["taskId"])

    def test_unverified_capabilities_refuse_work_and_keep_facts_unknown(self) -> None:
        status, _headers, capabilities = self.json_request("GET", "/capabilities")
        self.assertEqual(200, status)
        self.assertEqual(1, capabilities["protocolVersion"])
        self.assertTrue(capabilities["capabilities"])
        for capability in capabilities["capabilities"]:
            self.assertFalse(capability["available"])
            self.assertFalse(capability["verified"])
            self.assertIsNone(capability["providerId"])
            self.assertTrue(capability["reason"])

        _request, opened = self.open_task("request-synthetic-capability-task")
        task_id = opened["result"]["taskId"]
        refusal_status, refusal = self.command(
            envelope("observe", "request-synthetic-observe-refusal", {"taskId": task_id})
        )
        self.assertEqual(400, refusal_status)
        assert_result_envelope(self, refusal, expected_request_id="request-synthetic-observe-refusal")
        self.assertEqual("failed", refusal["status"])
        self.assertEqual("CAPABILITY_UNAVAILABLE", refusal["error"]["code"])
        self.assertFalse(refusal["runtimeChanged"])
        self.assertTrue(all(value is None for value in refusal["facts"].values()))
        self.assertEqual([], refusal["artifacts"])
        self.assertIsNone(refusal["jobId"])
        self.assertIsNone(refusal["planId"])

    def test_current_stateless_mcp_lists_formal_tools_and_calls_the_same_host(self) -> None:
        metadata = modern_mcp_meta()
        completed, responses = self.run_mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 501,
                    "method": "tools/list",
                    "params": {"_meta": metadata},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 502,
                    "method": "tools/call",
                    "params": {
                        "_meta": metadata,
                        "name": "relay_liveloop_status",
                        "arguments": {"requestId": "request-synthetic-modern-mcp-status"},
                    },
                },
            ]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual(2, len(completed.stdout.splitlines()), completed.stdout)
        self.assertEqual([501, 502], [response["id"] for response in responses])

        operations_document = json.loads(
            (self.contract_root / "operations.json").read_text(encoding="utf-8")
        )
        result_schema = json.loads(
            (self.contract_root / "result.schema.json").read_text(encoding="utf-8")
        )
        operation_records = operations_document["operations"]
        expected_names = sorted(
            "relay_liveloop_" + record["name"].replace(".", "_")
            for record in operation_records
        )
        listed_result = responses[0]["result"]
        self.assertEqual("complete", listed_result["resultType"])
        self.assertEqual("public", listed_result["cacheScope"])
        self.assertEqual("2026-07-28", listed_result["_meta"]["io.modelcontextprotocol/protocolVersion"])
        tools = listed_result["tools"]
        self.assertEqual(expected_names, [tool["name"] for tool in tools])
        by_name = {tool["name"]: tool for tool in tools}
        for record in operation_records:
            tool = by_name["relay_liveloop_" + record["name"].replace(".", "_")]
            self.assertEqual(result_schema, tool["outputSchema"])
            self.assertEqual(False, tool["inputSchema"]["additionalProperties"])
            self.assertEqual(
                {"requestId", *record["requiredArguments"]},
                set(tool["inputSchema"]["required"]),
            )

        call_result = responses[1]["result"]
        self.assertEqual("complete", call_result["resultType"])
        self.assertFalse(call_result["isError"])
        structured = call_result["structuredContent"]
        assert_result_envelope(
            self,
            structured,
            expected_request_id="request-synthetic-modern-mcp-status",
        )
        self.assertEqual(structured, json.loads(call_result["content"][0]["text"]))
        self.assertEqual("Relay LiveLoop", structured["result"]["service"])

    def test_classic_mcp_initialization_boundary_and_cross_transport_idempotency(self) -> None:
        open_arguments = {
            "requestId": "request-synthetic-classic-mcp-open",
            **task_arguments("Open one synthetic task through classic MCP."),
        }
        completed, responses = self.run_mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 601,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "synthetic-classic-client",
                            "version": "1.0",
                        },
                    },
                },
                {"jsonrpc": "2.0", "id": 602, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 603, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 604,
                    "method": "tools/call",
                    "params": {
                        "name": "relay_liveloop_task_open",
                        "arguments": open_arguments,
                    },
                },
            ]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual(4, len(completed.stdout.splitlines()), completed.stdout)
        self.assertEqual([601, 602, 603, 604], [response["id"] for response in responses])
        self.assertEqual("2025-11-25", responses[0]["result"]["protocolVersion"])
        self.assertEqual(-32002, responses[1]["error"]["code"])
        self.assertEqual(21, len(responses[2]["result"]["tools"]))

        tool_result = responses[3]["result"]
        self.assertFalse(tool_result["isError"])
        mcp_host_result = tool_result["structuredContent"]
        assert_result_envelope(
            self,
            mcp_host_result,
            expected_request_id="request-synthetic-classic-mcp-open",
        )
        http_envelope = envelope(
            "task.open",
            open_arguments["requestId"],
            {key: value for key, value in open_arguments.items() if key != "requestId"},
        )
        http_status, http_replay = self.command(http_envelope)
        self.assertEqual(200, http_status)
        self.assertEqual(mcp_host_result, http_replay)

        task_id = mcp_host_result["result"]["taskId"]
        shown = self.run_cli(
            "task.show",
            "--task",
            task_id,
            "--url",
            self.base_url,
            "--request-id",
            "request-synthetic-cli-after-mcp",
            "--json",
        )
        self.assertEqual(0, shown.returncode, shown.stderr)
        shown_result = json.loads(shown.stdout)
        self.assertEqual(task_id, shown_result["result"]["task"]["taskId"])
        self.assertEqual(
            "Open one synthetic task through classic MCP.",
            shown_result["result"]["task"]["goal"],
        )

    def test_mcp_protocol_and_tool_errors_remain_distinct(self) -> None:
        _request, opened = self.open_task("request-synthetic-mcp-error-task")
        task_id = opened["result"]["taskId"]
        metadata = modern_mcp_meta()
        completed, responses = self.run_mcp(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 701,
                    "method": "tools/call",
                    "params": {
                        "_meta": metadata,
                        "name": "relay_liveloop_unknown",
                        "arguments": {"requestId": "request-synthetic-unknown-tool"},
                    },
                },
                {"jsonrpc": "1.0", "id": 702, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 703,
                    "method": "tools/call",
                    "params": {
                        "_meta": metadata,
                        "name": "relay_liveloop_prepare",
                        "arguments": {
                            "requestId": "request-synthetic-mcp-prepare-refusal",
                            "taskId": task_id,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 704,
                    "method": "tools/call",
                    "params": {
                        "_meta": metadata,
                        "name": "relay_liveloop_iterate",
                        "arguments": {
                            "requestId": "request-synthetic-mcp-iterate-refusal",
                            "taskId": task_id,
                        },
                    },
                },
            ]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(4, len(completed.stdout.splitlines()), completed.stdout)
        self.assertEqual(-32602, responses[0]["error"]["code"])
        self.assertEqual(-32600, responses[1]["error"]["code"])

        for response, request_id in (
            (responses[2], "request-synthetic-mcp-prepare-refusal"),
            (responses[3], "request-synthetic-mcp-iterate-refusal"),
        ):
            with self.subTest(request_id=request_id):
                tool_error = response["result"]
                self.assertTrue(tool_error["isError"])
                structured = tool_error["structuredContent"]
                assert_result_envelope(self, structured, expected_request_id=request_id)
                self.assertEqual("CAPABILITY_UNAVAILABLE", structured["error"]["code"])
                self.assertFalse(structured["runtimeChanged"])
                self.assertTrue(all(value is None for value in structured["facts"].values()))
                self.assertEqual(structured, json.loads(tool_error["content"][0]["text"]))

    def test_artifact_route_blocks_traversal_and_detects_hash_tampering(self) -> None:
        original = b"synthetic artifact evidence\n"
        artifact_path = self.artifact_root / "synthetic-evidence.txt"
        artifact_path.write_bytes(original)
        metadata = self.seed_artifact(artifact_path)

        status, headers, downloaded = self.http_request(
            "GET", f"/artifacts/{metadata['artifactId']}"
        )
        self.assertEqual(200, status)
        self.assertEqual(original, downloaded)
        self.assertEqual(metadata["artifactId"], headers["x-artifact-id"])
        self.assertEqual(hashlib.sha256(original).hexdigest(), headers["x-artifact-sha256"])

        outside_marker = b"synthetic outside-root marker"
        (self.root / "outside-marker.txt").write_bytes(outside_marker)
        for path in ("/artifacts/../outside-marker.txt", "/artifacts/..%2Foutside-marker.txt"):
            with self.subTest(path=path):
                traversal_status, _traversal_headers, traversal_body = self.http_request("GET", path)
                self.assertIn(traversal_status, {400, 404})
                self.assertNotIn(outside_marker, traversal_body)
                traversal_error = json.loads(traversal_body.decode("utf-8"))
                self.assertEqual("CONTRACT_MISMATCH", traversal_error["error"]["code"])

        artifact_path.write_bytes(b"synthetic tampered bytes\n")
        tamper_status, _tamper_headers, tamper_body = self.http_request(
            "GET", f"/artifacts/{metadata['artifactId']}"
        )
        self.assertEqual(409, tamper_status)
        tamper_error = json.loads(tamper_body.decode("utf-8"))
        self.assertEqual("failed", tamper_error["status"])
        self.assertEqual("INPUT_CHANGED", tamper_error["error"]["code"])
        self.assertEqual("artifact", tamper_error["error"]["stage"])
        self.assertFalse(tamper_error["error"]["recoverable"])
        self.assertNotIn(b"synthetic tampered bytes", tamper_body)

    def test_job_status_and_cancellation_respect_state_boundaries_and_ids(self) -> None:
        self.seed_jobs()

        status_code, status_result = self.command(
            envelope(
                "job.status",
                "request-synthetic-job-status",
                {"jobId": "job-synthetic-cancellable"},
            )
        )
        self.assertEqual(200, status_code)
        assert_result_envelope(self, status_result, expected_request_id="request-synthetic-job-status")
        self.assertEqual("job-synthetic-cancellable", status_result["jobId"])
        self.assertEqual(
            "origin-synthetic-cancellable",
            status_result["result"]["job"]["requestId"],
        )

        cancellation_cases = [
            ("job-synthetic-cancellable", True, None),
            ("job-synthetic-applying", False, "non_interruptible_stage"),
            ("job-synthetic-terminal", False, "terminal"),
        ]
        for index, (job_id, accepted, reason) in enumerate(cancellation_cases, start=1):
            with self.subTest(job_id=job_id):
                request_id = f"request-synthetic-job-cancel-{index}"
                response_status, response = self.command(
                    envelope("job.cancel", request_id, {"jobId": job_id})
                )
                self.assertEqual(200, response_status)
                assert_result_envelope(self, response, expected_request_id=request_id)
                self.assertEqual(job_id, response["jobId"])
                self.assertEqual(accepted, response["result"]["cancelAccepted"])
                self.assertEqual(reason, response["result"]["reason"])
                self.assertEqual(accepted, response["result"]["job"]["cancelRequested"])

        direct_status, _direct_headers, direct_job = self.json_request(
            "GET", "/jobs/job-synthetic-cancellable"
        )
        self.assertEqual(200, direct_status)
        self.assertEqual("job-synthetic-cancellable", direct_job["job"]["jobId"])
        self.assertTrue(direct_job["job"]["cancelRequested"])

    def test_cli_rejects_conflicting_task_identifiers_before_network_send(self) -> None:
        completed = self.run_cli(
            "task.show",
            "--task",
            "task-synthetic-outer",
            "--arguments",
            '{"taskId":"task-synthetic-inner"}',
            "--url",
            self.base_url,
            "--json",
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("--task conflicts with arguments.taskId", completed.stderr)
        self.assertNotIn(self.token, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
