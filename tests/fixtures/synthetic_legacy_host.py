from __future__ import annotations

import argparse
import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LegacyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        expected = "Bearer " + self.server.bearer_token  # type: ignore[attr-defined]
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self._send(HTTPStatus.UNAUTHORIZED, {"status": "failed", "error": {"code": "AUTH_REQUIRED"}})
            return
        if self.path == "/status":
            self._send(
                HTTPStatus.OK,
                {
                    "service": "Relay LiveLoop",
                    "protocolVersion": 1,
                    "ledger": {"counts": {}, "commandsRequiringReconciliation": 0},
                    "capabilities": [],
                    "runtime": {
                        "sessionId": None,
                        "runtimeRevision": None,
                        "moduleGeneration": None,
                        "resourceRelease": None,
                        "viewGeneration": None,
                    },
                },
            )
            return
        if self.path == "/capabilities":
            self._send(HTTPStatus.OK, {"protocolVersion": 1, "capabilities": []})
            return
        self._send(HTTPStatus.NOT_FOUND, {"status": "failed", "error": {"code": "CONTRACT_MISMATCH"}})

    def do_POST(self) -> None:
        self._send(HTTPStatus.NOT_FOUND, {"status": "failed", "error": {"code": "CONTRACT_MISMATCH"}})

    def _send(self, status: HTTPStatus, value: dict[str, object]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("synthetic token is empty")
    server = ThreadingHTTPServer((args.host, args.port), LegacyHandler)
    server.bearer_token = token  # type: ignore[attr-defined]
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
