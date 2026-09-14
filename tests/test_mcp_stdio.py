from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from api.http_server import create_http_server
from clients.http_client import RelayHTTPClient
from host.artifacts import ArtifactStore
from host.ledger import Ledger
from host.service import CommandService
from mcp.stdio_server import CURRENT_PROTOCOL, MCPServer, ToolCatalog


def modern_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": CURRENT_PROTOCOL,
        "io.modelcontextprotocol/clientInfo": {"name": "synthetic-client", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


class MCPStdioTests(unittest.TestCase):
    def setUp(self) -> None:
        contract_root_value = os.environ.get("RELAY_LIVELOOP_CONTRACT_ROOT")
        if not contract_root_value:
            self.fail("RELAY_LIVELOOP_CONTRACT_ROOT must point to the public v1 contracts for this overlay test.")
        self.contract_root = Path(contract_root_value)
        self.catalog = ToolCatalog.load(
            self.contract_root / "operations.json",
            self.contract_root / "result.schema.json",
        )
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        self.ledger = Ledger(root / "host.sqlite3")
        self.service = CommandService(self.ledger, ArtifactStore(self.ledger, [artifacts]))
        self.token = "synthetic-mcp-token"
        self.http = create_http_server(self.service, self.token, port=0)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        host, port = self.http.server_address
        self.url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=5)
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def test_catalog_exposes_all_21_formal_operations(self) -> None:
        tools = self.catalog.tools
        self.assertEqual(21, len(tools))
        self.assertEqual(sorted(item["name"] for item in tools), [item["name"] for item in tools])
        self.assertTrue(all(item["name"].startswith("relay_liveloop_") for item in tools))
        self.assertTrue(all("requestId" in item["inputSchema"]["required"] for item in tools))

    def test_current_stateless_tools_list_and_call(self) -> None:
        server = MCPServer(RelayHTTPClient(self.url, self.token), self.catalog)
        listed = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": modern_meta()},
            }
        )
        self.assertEqual("complete", listed["result"]["resultType"])
        self.assertEqual(21, len(listed["result"]["tools"]))
        called = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": modern_meta(),
                    "name": "relay_liveloop_status",
                    "arguments": {"requestId": "request_mcp_modern_status"},
                },
            }
        )
        self.assertFalse(called["result"]["isError"])
        self.assertEqual("completed", called["result"]["structuredContent"]["status"])
        self.assertEqual(CURRENT_PROTOCOL, called["result"]["_meta"]["io.modelcontextprotocol/protocolVersion"])

    def test_legacy_requires_initialize_completion(self) -> None:
        server = MCPServer(RelayHTTPClient(self.url, self.token), self.catalog)
        premature = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(-32002, premature["error"]["code"])
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1.0"},
                },
            }
        )
        self.assertEqual("2025-11-25", initialized["result"]["protocolVersion"])
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        listed = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        self.assertEqual(21, len(listed["result"]["tools"]))

    def test_actual_stdio_process_uses_newline_json_and_no_extra_stdout(self) -> None:
        script = Path(__file__).resolve().parents[1] / "mcp" / "stdio_server.py"
        environment = os.environ.copy()
        environment["RELAY_LIVELOOP_TOKEN"] = self.token
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-process-client", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "relay_liveloop_status",
                    "arguments": {"requestId": "request_mcp_process_status"},
                },
            },
        ]
        wire = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in messages)
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--url",
                self.url,
                "--operations",
                str(self.contract_root / "operations.json"),
                "--result-schema",
                str(self.contract_root / "result.schema.json"),
            ],
            input=wire,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            env=environment,
            cwd=script.parents[1],
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(3, len(lines), completed.stdout)
        responses = [json.loads(line) for line in lines]
        self.assertEqual([1, 2, 3], [item["id"] for item in responses])
        self.assertEqual(21, len(responses[1]["result"]["tools"]))
        self.assertEqual("completed", responses[2]["result"]["structuredContent"]["status"])


if __name__ == "__main__":
    unittest.main()
