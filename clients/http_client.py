from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class RelayHTTPError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, response: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.response = response


class RelayHTTPClient:
    def __init__(self, base_url: str, bearer_token: str, *, timeout_seconds: float = 30.0):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("base_url must be loopback HTTP.")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials, query, or fragment data.")
        if not bearer_token:
            raise ValueError("A non-empty bearer token is required.")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be in (0, 3600].")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
        request_id: str | None = None,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {self.bearer_token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if isinstance(request_id, str) and CORRELATION_ID_RE.fullmatch(request_id):
            headers["X-Relay-Request-Id"] = request_id
        request = Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            raise RelayHTTPError(f"Relay LiveLoop HTTP returned {exc.code}.", status=exc.code, response=parsed) from exc
        except URLError as exc:
            raise RelayHTTPError("Relay LiveLoop HTTP is unreachable.") from exc

    def command(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        raw = self._request("POST", "/commands", payload, "application/json", envelope.get("requestId"))
        return json.loads(raw.decode("utf-8"))

    def status(self) -> dict[str, Any]:
        return json.loads(self._request("GET", "/status").decode("utf-8"))

    def capabilities(self) -> dict[str, Any]:
        return json.loads(self._request("GET", "/capabilities").decode("utf-8"))

    def shutdown(self, request_id: str, *, wait_for_active_jobs: bool) -> dict[str, Any]:
        if not isinstance(request_id, str) or not CORRELATION_ID_RE.fullmatch(request_id):
            raise ValueError("request_id is invalid.")
        if type(wait_for_active_jobs) is not bool:
            raise ValueError("wait_for_active_jobs must be a boolean.")
        payload = json.dumps(
            {
                "protocolVersion": 1,
                "requestId": request_id,
                "mode": "graceful",
                "preservePlayer": True,
                "waitForActiveJobs": wait_for_active_jobs,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        raw = self._request("POST", "/lifecycle/shutdown", payload, "application/json", request_id)
        return json.loads(raw.decode("utf-8"))

    def job(self, job_id: str) -> dict[str, Any]:
        return json.loads(self._request("GET", f"/jobs/{job_id}").decode("utf-8"))

    def artifact(self, artifact_id: str) -> bytes:
        return self._request("GET", f"/artifacts/{artifact_id}")
