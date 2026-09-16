from __future__ import annotations

import copy
import json
import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PUBLIC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PUBLIC_ROOT))

from api.http_server import RelayRequestHandler  # noqa: E402
from clients.http_client import RelayHTTPError  # noqa: E402
from host.artifacts import ArtifactStore  # noqa: E402
from host.errors import CommandError  # noqa: E402
from host.ledger import Ledger  # noqa: E402
from host.providers import ProviderRegistry  # noqa: E402
from host.runtime_transport import LoopbackRuntimeHostTransport, RuntimeTransportReply  # noqa: E402
from host.service import CommandService  # noqa: E402
from host.validation import validate_command  # noqa: E402
from mcp.stdio_server import MCPServer, ToolCatalog  # noqa: E402
import relay_liveloop  # noqa: E402


ALLOWED_IMPACT = {
    "hotfix": False,
    "rebuildViews": [],
    "reloadModules": [],
    "restartPlayer": False,
    "buildBaseline": False,
}
CONTEXT = {
    "sessionId": "session-a",
    "expectedRuntimeRevision": "revision-a",
    "expectedLaunchId": "launch-a",
}


class RecordingProvider:
    capability = "verification"

    def __init__(self) -> None:
        self.commands: list[dict] = []

    def execute(self, operation: str, command: dict) -> dict:
        self.commands.append(copy.deepcopy(command))
        return {
            "status": "completed",
            "result": {
                "operation": operation,
                "wireRequestId": command["requestId"],
                "wireTaskId": command["taskId"],
                "wireContext": command["context"],
            },
            "runtimeChanged": False,
        }


class RecordingMcpClient:
    def __init__(self) -> None:
        self.commands: list[dict] = []

    def command(self, command: dict) -> dict:
        self.commands.append(copy.deepcopy(command))
        return {"requestId": command["requestId"], "status": "completed", "result": {}}


class RecordingCliClient:
    last_command: dict | None = None

    def __init__(self, url: str, token: str, timeout_seconds: float) -> None:
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def command(self, command: dict) -> dict:
        type(self).last_command = copy.deepcopy(command)
        return {"requestId": command["requestId"], "status": "completed", "result": {}}


def command(request_id: str, operation: str, arguments: dict, *, task_id: str | None = "task-a", context: dict | None = None) -> dict:
    value = {
        "protocolVersion": 1,
        "requestId": request_id,
        "operation": operation,
        "arguments": copy.deepcopy(arguments),
    }
    if task_id is not None:
        value["taskId"] = task_id
    if context is not None:
        value["context"] = copy.deepcopy(context)
    return value


def assert_equal(expected, actual, message: str) -> None:
    if expected != actual:
        raise AssertionError(f"{message}: expected={expected!r} actual={actual!r}")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def expect_error(action, code: str, message: str) -> CommandError:
    try:
        action()
    except CommandError as error:
        assert_equal(code, error.code, message + " code")
        return error
    raise AssertionError(message + " did not fail")


def task_open_command() -> dict:
    return command(
        "open-request",
        "task.open",
        {
            "goal": "source contract repair",
            "sessionId": "session-a",
            "target": "neutral synthetic target",
            "allowedImpact": ALLOWED_IMPACT,
            "acceptance": ["wire identity is preserved"],
        },
        task_id=None,
    )


def input_text_command(request_id: str = "input-request", *, task_id: str = "task-a", context: dict | None = None) -> dict:
    return command(
        request_id,
        "input.text",
        {
            "targetId": "target-a",
            "expectedOwnerGeneration": 4,
            "expectedFrame": 17,
            "expectedViewportGeneration": 3,
            "text": "hello",
        },
        task_id=task_id,
        context=CONTEXT if context is None else context,
    )


def input_click_command(request_id: str = "click-request", *, task_id: str = "task-a", context: dict | None = None) -> dict:
    return command(
        request_id,
        "input.click",
        {
            "targetId": "target-a",
            "expectedOwnerGeneration": 4,
            "expectedFrame": 17,
            "expectedViewportGeneration": 3,
            "screenX": 12.5,
            "screenY": 24.5,
        },
        task_id=task_id,
        context=CONTEXT if context is None else context,
    )


def test_validation_and_common_service() -> None:
    normalized = validate_command(input_text_command())
    assert_equal("launch-a", normalized["context"]["expectedLaunchId"], "validator accepts expectedLaunchId")
    assert_true("taskId" not in normalized["arguments"], "validator moves taskId to the envelope")
    click_normalized = validate_command(input_click_command())
    assert_equal("input.click", click_normalized["operation"], "validator accepts input.click")

    with tempfile.TemporaryDirectory(prefix="relay-liveloop-public-test-") as directory:
        ledger = Ledger(Path(directory) / "ledger.sqlite3")
        artifact_root = Path(directory) / "artifacts"
        artifact_root.mkdir()
        artifacts = ArtifactStore(ledger, [artifact_root])
        providers = ProviderRegistry()
        provider = RecordingProvider()
        providers.register("verification", "synthetic-verification", provider, verified=True)
        service = CommandService(ledger, artifacts, providers=providers)
        try:
            opened = service.execute(task_open_command())
            assert_equal("completed", opened["status"], "task.open succeeds")
            task_id = opened["result"]["taskId"]

            first = input_text_command("input-request")
            first["taskId"] = task_id
            first_result = service.execute(first)
            assert_equal("completed", first_result["status"], "input command reaches common service")
            assert_equal(task_id, provider.commands[0]["taskId"], "input taskId reaches provider envelope")
            assert_equal("launch-a", provider.commands[0]["context"]["expectedLaunchId"], "input launch context reaches provider")
            assert_true("taskId" not in provider.commands[0]["arguments"], "provider receives one canonical task binding")

            click = input_click_command("click-request", task_id=task_id)
            click_result = service.execute(click)
            assert_equal("completed", click_result["status"], "input.click reaches common service")
            assert_equal("input.click", provider.commands[1]["operation"], "common service keeps click operation")

            mismatch = input_text_command("input-mismatch", task_id=task_id)
            mismatch["arguments"]["taskId"] = "another-task"
            mismatch_result = service.execute(mismatch)
            assert_equal("failed", mismatch_result["status"], "task mismatch is terminal failure")
            assert_equal("CONTRACT_MISMATCH", mismatch_result["error"]["code"], "task mismatch code")
            assert_equal(2, len(provider.commands), "task mismatch never reaches provider")
        finally:
            service.close()
            ledger.close()


def test_runtime_transport_identity_and_wire_payload() -> None:
    transport = LoopbackRuntimeHostTransport(
        shared_secret=b"x" * 32,
        expected_session_id="session-a",
        expected_launch_id="launch-a",
        expected_runtime_revision="revision-a",
        port=0,
    )
    calls: list[dict] = []

    def invoke(*, request_id: str, operation: str, payload: bytes, timeout_seconds=None, expected_context=None) -> RuntimeTransportReply:
        calls.append({"requestId": request_id, "operation": operation, "payload": json.loads(payload.decode("utf-8"))})
        return RuntimeTransportReply(
            request_id=request_id,
            schema_id="relay.liveloop.command-result",
            schema_version=1,
            media_type="application/json",
            payload=b'{"runtimeChanged":false}',
            runtime_changed=False,
            runtime_revision_after="revision-a",
        )

    transport.invoke = invoke  # type: ignore[method-assign]
    result = transport.execute("input.text", input_text_command())
    assert_equal(False, result["runtimeChanged"], "transport returns Player result")
    assert_equal("launch-a", calls[0]["payload"]["context"]["expectedLaunchId"], "wire payload carries launch identity")
    assert_equal("task-a", calls[0]["payload"]["taskId"], "wire payload carries task identity")

    click_result = transport.execute("input.click", input_click_command())
    assert_equal(False, click_result["runtimeChanged"], "click transport returns Player result")
    assert_equal("input.click", calls[1]["operation"], "click reaches the same transport")

    old_launch = input_text_command("old-launch", context={**CONTEXT, "expectedLaunchId": "launch-old"})
    expect_error(lambda: transport.execute("input.text", old_launch), "WRONG_SESSION", "transport rejects another launch")
    old_session = input_text_command("old-session", context={**CONTEXT, "sessionId": "session-old"})
    expect_error(lambda: transport.execute("input.text", old_session), "WRONG_SESSION", "transport rejects another session")
    old_revision = input_text_command("old-revision", context={**CONTEXT, "expectedRuntimeRevision": "revision-old"})
    expect_error(lambda: transport.execute("input.text", old_revision), "STALE_TARGET", "transport rejects another revision")
    missing_session = input_text_command("missing-session", context={"expectedRuntimeRevision": "revision-a", "expectedLaunchId": "launch-a"})
    expect_error(lambda: transport.execute("input.text", missing_session), "INVALID_REQUEST", "transport requires session identity")
    missing_revision = input_text_command("missing-revision", context={"sessionId": "session-a", "expectedLaunchId": "launch-a"})
    expect_error(lambda: transport.execute("input.text", missing_revision), "INVALID_REQUEST", "transport requires runtime revision")
    missing_launch = input_text_command("missing-launch", context={"sessionId": "session-a", "expectedRuntimeRevision": "revision-a"})
    expect_error(lambda: transport.execute("input.text", missing_launch), "INVALID_REQUEST", "transport requires launch identity")
    assert_equal(2, len(calls), "rejected identity commands never invoke the Player transport")


def test_mcp_and_cli_adapters_preserve_context() -> None:
    root = PUBLIC_ROOT
    catalog = ToolCatalog.load(root / "contracts" / "operations.json", root / "contracts" / "result.schema.json")
    observe_tool = next(tool for tool in catalog.tools if tool["name"] == "relay_liveloop_observe")
    assert_true("expectedLaunchId" in observe_tool["inputSchema"]["properties"]["context"]["properties"], "MCP schema exposes launch identity")
    assert_true(any(tool["name"] == "relay_liveloop_input_click" for tool in catalog.tools), "MCP catalog registers input.click")
    assert_true(any(tool["name"] == "relay_liveloop_input_text" for tool in catalog.tools), "MCP catalog registers input.text")

    mcp_client = RecordingMcpClient()
    server = MCPServer(mcp_client, catalog)
    modern_meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "synthetic", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    mcp_result = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "relay_liveloop_observe",
            "arguments": {"requestId": "mcp-request", "taskId": "task-a", "context": CONTEXT},
            "_meta": modern_meta,
        },
    })
    assert_true(mcp_result is not None, "MCP returns a JSON-RPC response")
    assert_equal("launch-a", mcp_client.commands[0]["context"]["expectedLaunchId"], "MCP preserves launch context")
    assert_equal("task-a", mcp_client.commands[0]["taskId"], "MCP preserves task envelope")

    for index, (tool_name, arguments, operation) in enumerate((
        ("relay_liveloop_input_click", {"targetId": "target-a", "expectedOwnerGeneration": 4, "expectedFrame": 17, "expectedViewportGeneration": 3, "screenX": 12.5, "screenY": 24.5}, "input.click"),
        ("relay_liveloop_input_text", {"targetId": "target-a", "expectedOwnerGeneration": 4, "expectedFrame": 17, "expectedViewportGeneration": 3, "text": "hello"}, "input.text"),
    ), start=1):
        arguments.update({"requestId": f"mcp-input-{index}", "taskId": "task-a", "context": CONTEXT})
        result = server.handle({
            "jsonrpc": "2.0",
            "id": index + 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments, "_meta": modern_meta},
        })
        assert_true(result is not None, f"MCP returns {operation} response")
        assert_equal(operation, mcp_client.commands[index]["operation"], f"MCP routes {operation} through client")

    assert_true("self.server.service.execute(payload)" in inspect.getsource(RelayRequestHandler.do_POST), "HTTP adapter delegates to common CommandService")

    original_token = relay_liveloop._token_from_args
    original_client = relay_liveloop.RelayHTTPClient
    try:
        relay_liveloop._token_from_args = lambda args: "synthetic-token"
        relay_liveloop.RelayHTTPClient = RecordingCliClient
        args = SimpleNamespace(
            file=None,
            arguments=json.dumps({"targetId": "target-a", "expectedOwnerGeneration": 4, "expectedFrame": 17, "expectedViewportGeneration": 3, "text": "hello"}),
            context=json.dumps(CONTEXT),
            request_id="cli-request",
            operation="input.text",
            task="task-a",
            url="http://127.0.0.1:1",
            timeout=1.0,
            json=True,
        )
        assert_equal(0, relay_liveloop._send(args), "CLI adapter sends a successful result")
        assert_equal("launch-a", RecordingCliClient.last_command["context"]["expectedLaunchId"], "CLI preserves launch context")
        assert_equal("task-a", RecordingCliClient.last_command["taskId"], "CLI preserves task envelope")
        assert_equal("input.text", RecordingCliClient.last_command["operation"], "CLI routes input.text")
        assert_equal("hello", RecordingCliClient.last_command["arguments"]["text"], "CLI preserves input.text arguments")
    finally:
        relay_liveloop._token_from_args = original_token
        relay_liveloop.RelayHTTPClient = original_client


def main() -> int:
    test_validation_and_common_service()
    test_runtime_transport_identity_and_wire_payload()
    test_mcp_and_cli_adapters_preserve_context()
    print("HOST SOURCE REPAIR PASS: validator, common service, runtime transport, MCP, and CLI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
