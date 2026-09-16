from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.http_client import RelayHTTPClient, RelayHTTPError

CURRENT_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "relay-liveloop", "title": "Relay LiveLoop", "version": "0.1.0"}
TOOL_PREFIX = "relay_liveloop_"
MAX_MESSAGE_BYTES = 1024 * 1024

ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "taskId": {"type": "string", "minLength": 1, "maxLength": 128},
    "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
    "sessionId": {"type": "string", "minLength": 1, "maxLength": 128},
    "target": {"type": "string", "minLength": 1, "maxLength": 2000},
    "allowedImpact": {"type": "object"},
    "acceptance": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string"}},
    "reference": {"type": ["string", "null"], "maxLength": 4000},
    "updates": {"type": "object", "minProperties": 1},
    "edits": {"type": "array", "minItems": 1, "maxItems": 128, "items": {"type": "object"}},
    "expectedSourceHash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "changes": {"type": "object", "minProperties": 1},
    "expected": {"type": "object", "minProperties": 1},
    "overlayId": {"type": "string", "minLength": 1, "maxLength": 128},
    "planId": {"type": "string", "minLength": 1, "maxLength": 128},
    "userConfirmationRef": {"type": "string", "minLength": 1, "maxLength": 1000},
    "approvedImpact": {"type": "object"},
    "checkSetId": {"type": "string", "minLength": 1, "maxLength": 128},
    "sourceSnapshot": {"type": "string", "minLength": 1, "maxLength": 128},
    "profileId": {"type": "string", "minLength": 1, "maxLength": 128},
    "authorizationRef": {"type": "string", "minLength": 1, "maxLength": 128},
    "artifactId": {"type": "string", "minLength": 1, "maxLength": 128},
    "baselineId": {"type": "string", "minLength": 1, "maxLength": 128},
    "jobId": {"type": "string", "minLength": 1, "maxLength": 128},
    "inputSnapshot": {"type": "string", "minLength": 1, "maxLength": 128},
    "expectedRuntimeRevision": {"type": "string", "minLength": 1, "maxLength": 128},
    "targetId": {"type": "string", "minLength": 1, "maxLength": 128},
    "expectedOwnerGeneration": {"type": "integer"},
    "expectedFrame": {"type": "integer"},
    "expectedViewportGeneration": {"type": "integer"},
    "screenX": {"type": "number"},
    "screenY": {"type": "number"},
    "text": {"type": "string", "maxLength": 4096},
    "checks": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "object"}},
}

OPTIONAL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "task.open": ("reference",),
    "prepare": ("inputSnapshot", "expectedRuntimeRevision"),
    "iterate": ("planId",),
    "verify": ("checks",),
}


class ToolCatalog:
    def __init__(self, operations: list[dict[str, Any]], result_schema: dict[str, Any]):
        self._operations: dict[str, dict[str, Any]] = {}
        self._tools: list[dict[str, Any]] = []
        for record in operations:
            self._add(record, result_schema)

    @classmethod
    def load(cls, operations_path: str | Path, result_schema_path: str | Path) -> "ToolCatalog":
        operations_document = json.loads(Path(operations_path).read_text(encoding="utf-8"))
        result_schema = json.loads(Path(result_schema_path).read_text(encoding="utf-8"))
        if operations_document.get("protocolVersion") != 1 or not isinstance(operations_document.get("operations"), list):
            raise ValueError("operations catalog is not protocol version 1.")
        if not isinstance(result_schema, dict) or result_schema.get("type") != "object":
            raise ValueError("result schema must be a JSON Schema object.")
        return cls(operations_document["operations"], result_schema)

    def _add(self, record: dict[str, Any], result_schema: dict[str, Any]) -> None:
        required_record = {"name", "requiredArguments", "effect", "result"}
        if not isinstance(record, dict) or set(record) != required_record:
            raise ValueError("operation record fields do not match protocol v1.")
        operation = record["name"]
        required = record["requiredArguments"]
        if not isinstance(operation, str) or not isinstance(required, list) or operation in self._operations:
            raise ValueError("operation catalog contains an invalid or duplicate operation.")
        tool_name = TOOL_PREFIX + operation.replace(".", "_")
        properties: dict[str, Any] = {
            "requestId": {"type": "string", "minLength": 1, "maxLength": 128},
            "context": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "projectId": {"type": "string", "minLength": 1},
                    "workspaceId": {"type": "string", "minLength": 1},
                    "sessionId": {"type": "string", "minLength": 1},
                    "expectedRuntimeRevision": {"type": "string", "minLength": 1},
                    "expectedLaunchId": {"type": "string", "minLength": 1, "maxLength": 128},
                },
            },
        }
        for name in required + list(OPTIONAL_ARGUMENTS.get(operation, ())):
            properties[name] = ARGUMENT_SCHEMAS.get(name, {"type": "object"})
        input_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["requestId", *required],
            "properties": properties,
        }
        read_only = record["effect"] == "read"
        tool = {
            "name": tool_name,
            "title": f"Relay LiveLoop {operation}",
            "description": f"Map the {operation} operation to the Relay LiveLoop Host CommandService.",
            "inputSchema": input_schema,
            "outputSchema": result_schema,
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": not read_only,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        self._operations[tool_name] = {"operation": operation, "record": record, "tool": tool}
        self._tools.append(tool)

    @property
    def tools(self) -> list[dict[str, Any]]:
        return sorted((dict(item) for item in self._tools), key=lambda item: item["name"])

    def operation_for(self, tool_name: str) -> str | None:
        record = self._operations.get(tool_name)
        return record["operation"] if record else None


class MCPServer:
    def __init__(self, client: RelayHTTPClient, catalog: ToolCatalog):
        self.client = client
        self.catalog = catalog
        self._legacy_initialized = False
        self._legacy_protocol: str | None = None

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            request_id = message.get("id") if isinstance(message, dict) else None
            return self._error(request_id, -32600, "Invalid JSON-RPC request.")
        method = message["method"]
        request_id = message.get("id")
        is_notification = "id" not in message
        params = message.get("params", {})
        if not isinstance(params, dict):
            return None if is_notification else self._error(request_id, -32602, "params must be an object.")

        if method == "initialize":
            if is_notification:
                return None
            return self._initialize(request_id, params)
        if method == "notifications/initialized":
            if not is_notification:
                return self._error(request_id, -32600, "notifications/initialized must not include an id.")
            if self._legacy_protocol is not None:
                self._legacy_initialized = True
            return None
        if method == "ping":
            if is_notification:
                return None
            if not self._request_ready(params):
                return self._error(request_id, -32002, "MCP request is not initialized or lacks current protocol metadata.")
            return self._response(request_id, self._modern_result({}, params))
        if is_notification:
            return None
        if not self._request_ready(params):
            return self._error(request_id, -32002, "MCP request is not initialized or lacks current protocol metadata.")
        if method == "tools/list":
            if params.get("cursor") is not None:
                return self._error(request_id, -32602, "This tool list has one page; cursor must be omitted.")
            result: dict[str, Any] = {"tools": self.catalog.tools}
            if self._is_modern(params):
                result.update({"resultType": "complete", "cacheScope": "public", "ttlMs": 300000})
            return self._response(request_id, self._modern_result(result, params))
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, -32601, "Method not found.")

    def _initialize(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) - {"protocolVersion", "capabilities", "clientInfo", "_meta"}:
            return self._error(request_id, -32602, "initialize contains unsupported fields.")
        version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client_info = params.get("clientInfo")
        if not isinstance(version, str) or not isinstance(capabilities, dict) or not isinstance(client_info, dict):
            return self._error(request_id, -32602, "initialize requires protocolVersion, capabilities, and clientInfo.")
        if not isinstance(client_info.get("name"), str) or not isinstance(client_info.get("version"), str):
            return self._error(request_id, -32602, "clientInfo requires name and version strings.")
        negotiated = version if version in LEGACY_PROTOCOLS else LEGACY_PROTOCOLS[0]
        self._legacy_protocol = negotiated
        self._legacy_initialized = False
        return self._response(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Relay LiveLoop tools map directly to the authenticated local Host CommandService.",
            },
        )

    @staticmethod
    def _is_modern(params: dict[str, Any]) -> bool:
        metadata = params.get("_meta")
        return isinstance(metadata, dict) and metadata.get("io.modelcontextprotocol/protocolVersion") == CURRENT_PROTOCOL

    def _request_ready(self, params: dict[str, Any]) -> bool:
        if self._legacy_initialized:
            return True
        metadata = params.get("_meta")
        if not isinstance(metadata, dict) or metadata.get("io.modelcontextprotocol/protocolVersion") != CURRENT_PROTOCOL:
            return False
        client_info = metadata.get("io.modelcontextprotocol/clientInfo")
        client_capabilities = metadata.get("io.modelcontextprotocol/clientCapabilities")
        return (
            isinstance(client_info, dict)
            and isinstance(client_info.get("name"), str)
            and isinstance(client_info.get("version"), str)
            and isinstance(client_capabilities, dict)
        )

    @staticmethod
    def _modern_result(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        if not MCPServer._is_modern(params):
            return result
        enriched = dict(result)
        enriched["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": CURRENT_PROTOCOL,
            "io.modelcontextprotocol/serverInfo": SERVER_INFO,
            "io.modelcontextprotocol/serverCapabilities": {"tools": {"listChanged": False}},
        }
        return enriched

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) - {"name", "arguments", "_meta"}:
            return self._error(request_id, -32602, "tools/call contains unsupported fields.")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._error(request_id, -32602, "tools/call requires a tool name and object arguments.")
        operation = self.catalog.operation_for(name)
        if operation is None:
            return self._error(request_id, -32602, "Unknown tool.")
        request_key = arguments.get("requestId")
        if not isinstance(request_key, str) or not request_key:
            return self._error(request_id, -32602, "Tool arguments require a non-empty requestId.")
        command_arguments = {key: value for key, value in arguments.items() if key not in {"requestId", "context"}}
        envelope: dict[str, Any] = {
            "protocolVersion": 1,
            "requestId": request_key,
            "operation": operation,
            "arguments": command_arguments,
        }
        if "taskId" in command_arguments:
            envelope["taskId"] = command_arguments["taskId"]
        if "context" in arguments:
            envelope["context"] = arguments["context"]
        try:
            host_result = self.client.command(envelope)
        except RelayHTTPError as exc:
            host_result = exc.response
            if host_result is None:
                return self._error(request_id, -32603, "Relay LiveLoop Host is unreachable.")
        text = json.dumps(host_result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        result = {
            "content": [{"type": "text", "text": text}],
            "structuredContent": host_result,
            "isError": host_result.get("status") in {"failed", "state_unknown"},
        }
        if self._is_modern(params):
            result["resultType"] = "complete"
        return self._response(request_id, self._modern_result(result, params))

    def serve(self, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
        while line := input_stream.readline(MAX_MESSAGE_BYTES + 1):
            if len(line) > MAX_MESSAGE_BYTES:
                response = self._error(None, -32700, "MCP message exceeds the configured limit.")
            else:
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = self._error(None, -32700, "Invalid newline-delimited UTF-8 JSON.")
                else:
                    response = self.handle(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n")
                output_stream.flush()


def _token(args: argparse.Namespace) -> str:
    if args.token_file:
        path = Path(args.token_file)
        if not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("token file is missing or too large.")
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get("RELAY_LIVELOOP_TOKEN", "")
    if not value:
        raise ValueError("RELAY_LIVELOOP_TOKEN or --token-file is required.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay-liveloop-mcp")
    parser.add_argument("--url", default=os.environ.get("RELAY_LIVELOOP_URL", "http://127.0.0.1:18760"))
    parser.add_argument("--token-file")
    parser.add_argument("--operations", default=str(Path(__file__).resolve().parents[1] / "contracts" / "operations.json"))
    parser.add_argument("--result-schema", default=str(Path(__file__).resolve().parents[1] / "contracts" / "result.schema.json"))
    args = parser.parse_args(argv)
    try:
        server = MCPServer(
            RelayHTTPClient(args.url, _token(args)),
            ToolCatalog.load(args.operations, args.result_schema),
        )
        server.serve(sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
