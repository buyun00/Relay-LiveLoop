from __future__ import annotations

import base64
import binascii
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeSessionConfig:
    session_id: str
    launch_id: str
    runtime_revision: str
    protocol_version: int
    shared_secret: bytes
    host_address: str
    port: int


def load_runtime_session(path: str | Path) -> RuntimeSessionConfig:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError("--runtime-session-file must refer to an existing regular file.")
    if candidate.stat().st_size > 16 * 1024:
        raise ValueError("--runtime-session-file exceeds 16384 bytes.")
    try:
        value: Any = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("--runtime-session-file is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("--runtime-session-file must contain a JSON object.")

    def required_text(key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result or "\n" in result or "\r" in result or len(result) > 256:
            raise ValueError(f"{key} must be a non-empty canonical text value.")
        return result

    encoded = value.get("sharedSecretBase64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("sharedSecretBase64 is required.")
    try:
        secret = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("sharedSecretBase64 must be strict Base64.") from exc
    if len(secret) < 32:
        raise ValueError("sharedSecretBase64 must decode to at least 32 bytes.")

    host_address = value.get("hostAddress", "127.0.0.1")
    try:
        address = ipaddress.ip_address(host_address)
    except ValueError as exc:
        raise ValueError("hostAddress must be a numeric loopback address.") from exc
    if not address.is_loopback:
        raise ValueError("hostAddress must be a numeric loopback address.")
    port = value.get("port", 18761)
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535.")
    protocol_version = value.get("protocolVersion", 1)
    if not isinstance(protocol_version, int) or protocol_version <= 0:
        raise ValueError("protocolVersion must be positive.")
    return RuntimeSessionConfig(
        required_text("sessionId"), required_text("launchId"),
        required_text("runtimeRevision"), protocol_version, secret,
        str(address), port,
    )
