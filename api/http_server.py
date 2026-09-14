from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from host.errors import CommandError
from host.service import CommandService
from host.validation import ID_RE, MAX_COMMAND_BYTES

MAX_AUTH_HEADER = 4096


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], service: CommandService, bearer_token: str):
        if not bearer_token:
            raise ValueError("A non-empty bearer token is required.")
        host = server_address[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Relay LiveLoop HTTP must bind to a loopback address.")
        self.service = service
        self.bearer_token = bearer_token
        super().__init__(server_address, RelayRequestHandler)


class RelayRequestHandler(BaseHTTPRequestHandler):
    server: RelayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Integrators may provide a structured sink. Avoid logging headers, tokens, or command bodies.
        return

    def _authorized(self) -> bool:
        raw = self.headers.get("Authorization", "")
        if len(raw) > MAX_AUTH_HEADER:
            return False
        prefix = "Bearer "
        return raw.startswith(prefix) and hmac.compare_digest(raw[len(prefix) :], self.server.bearer_token)

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _correlation_id(self) -> str:
        supplied = self.headers.get("X-Relay-Request-Id")
        if isinstance(supplied, str) and ID_RE.fullmatch(supplied):
            return supplied
        return f"http_{uuid4().hex}"

    def _error(self, status: int, error: CommandError, request_id: str | None = None) -> None:
        self._send_json(
            status,
            {
                "requestId": request_id or self._correlation_id(),
                "status": "failed" if error.code != "STATE_UNKNOWN" else "state_unknown",
                "jobId": None,
                "planId": None,
                "runtimeChanged": error.runtime_changed,
                "facts": {
                    "sourceSaved": None,
                    "runtimeMatched": None,
                    "checksPassed": None,
                    "visualReviewed": None,
                    "freshVerified": None,
                },
                "result": {},
                "error": error.as_dict(),
                "artifacts": [],
                "timingsMs": {},
            },
        )

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._error(
            HTTPStatus.UNAUTHORIZED,
            CommandError("AUTH_REQUIRED", "A valid local bearer token is required.", stage="http"),
        )
        return False

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        if path == "/status":
            self._send_json(HTTPStatus.OK, self.server.service.status_summary())
            return
        if path == "/capabilities":
            self._send_json(HTTPStatus.OK, self.server.service.capability_summary())
            return
        if path.startswith("/jobs/"):
            job_id = path.removeprefix("/jobs/")
            self._read_job(job_id)
            return
        if path.startswith("/artifacts/"):
            artifact_id = path.removeprefix("/artifacts/")
            self._read_artifact(artifact_id)
            return
        self._error(HTTPStatus.NOT_FOUND, CommandError("CONTRACT_MISMATCH", "Unknown HTTP route.", stage="http"))

    def _read_job(self, job_id: str) -> None:
        if not ID_RE.fullmatch(job_id):
            self._error(HTTPStatus.BAD_REQUEST, CommandError("CONTRACT_MISMATCH", "jobId is invalid.", stage="http"))
            return
        try:
            job = self.server.service.ledger.get_job(job_id)
        except CommandError as error:
            self._error(HTTPStatus.NOT_FOUND, error)
            return
        self._send_json(HTTPStatus.OK, {"job": job})

    def _read_artifact(self, artifact_id: str) -> None:
        try:
            metadata, stream = self.server.service.artifacts.open_verified(artifact_id)
        except CommandError as error:
            status = HTTPStatus.NOT_FOUND if error.stage == "artifact" and error.code == "CONTRACT_MISMATCH" else HTTPStatus.CONFLICT
            self._error(status, error)
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", metadata["mediaType"])
            self.send_header("Content-Length", str(metadata["size"]))
            self.send_header("X-Artifact-Id", metadata["artifactId"])
            self.send_header("X-Artifact-Sha256", metadata["sha256"])
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)
        finally:
            stream.close()

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        if path != "/commands":
            self._error(HTTPStatus.NOT_FOUND, CommandError("CONTRACT_MISMATCH", "Unknown HTTP route.", stage="http"))
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, CommandError("CONTRACT_MISMATCH", "Content-Type must be application/json.", stage="http"))
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_COMMAND_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, CommandError("CONTRACT_MISMATCH", "Command body size is missing or out of range.", stage="http"))
            return
        try:
            command = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, CommandError("CONTRACT_MISMATCH", "Command body is not valid UTF-8 JSON.", stage="http"))
            return
        response = self.server.service.execute(command)
        error_code = (response.get("error") or {}).get("code")
        status = HTTPStatus.OK
        if error_code == "AUTH_REQUIRED":
            status = HTTPStatus.FORBIDDEN
        elif response["status"] in {"failed", "state_unknown"}:
            status = HTTPStatus.CONFLICT if response["status"] == "state_unknown" else HTTPStatus.BAD_REQUEST
        self._send_json(status, response)


def create_http_server(
    service: CommandService,
    bearer_token: str,
    host: str = "127.0.0.1",
    port: int = 18760,
) -> RelayHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in 0..65535")
    return RelayHTTPServer((host, port), service, bearer_token)
