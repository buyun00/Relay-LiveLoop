from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

from .errors import CommandError


@dataclass(frozen=True, slots=True)
class RuntimeTransportReply:
    request_id: str
    schema_id: str
    schema_version: int
    media_type: str
    payload: bytes
    runtime_changed: bool
    runtime_revision_after: str


class LoopbackRuntimeHostTransport:
    """Host listener and command endpoint for one explicitly identified Player session.

    An authenticated TCP session proves transport connectivity only. It never marks Hotfix,
    Reload, observation, or another native provider capability as verified. Commands are sent
    once; a loss after send is returned as STATE_UNKNOWN and is never replayed automatically.
    """

    capability = "runtime_apply"

    def __init__(
        self,
        *,
        shared_secret: bytes,
        expected_session_id: str,
        expected_launch_id: str,
        expected_runtime_revision: str,
        protocol_version: int = 1,
        listen_address: str = "127.0.0.1",
        port: int = 18761,
        maximum_frame_bytes: int = 64 * 1024,
        handshake_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        address = ipaddress.ip_address(listen_address)
        if not address.is_loopback:
            raise ValueError("Runtime transport must listen on a numeric loopback address.")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535.")
        if len(shared_secret) < 32:
            raise ValueError("shared_secret must contain at least 32 bytes.")
        self._require_canonical(expected_session_id, "expected_session_id")
        self._require_canonical(expected_launch_id, "expected_launch_id")
        self._require_canonical(expected_runtime_revision, "expected_runtime_revision")
        if protocol_version <= 0:
            raise ValueError("protocol_version must be positive.")
        if maximum_frame_bytes < 256:
            raise ValueError("maximum_frame_bytes must be at least 256.")
        for label, value in (
            ("handshake_timeout_seconds", handshake_timeout_seconds),
            ("command_timeout_seconds", command_timeout_seconds),
            ("shutdown_timeout_seconds", shutdown_timeout_seconds),
        ):
            if not 0 < value <= 300:
                raise ValueError(f"{label} must be greater than zero and no more than 300.")

        self._shared_secret = bytearray(shared_secret)
        self._expected_session_id = expected_session_id
        self._expected_launch_id = expected_launch_id
        self._expected_runtime_revision = expected_runtime_revision
        self._protocol_version = protocol_version
        self._listen_address = str(address)
        self._requested_port = port
        self._maximum_frame_bytes = maximum_frame_bytes
        self._handshake_timeout = handshake_timeout_seconds
        self._command_timeout = command_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds

        self._state_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._condition = threading.Condition(self._state_lock)
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._handshake_threads: set[threading.Thread] = set()
        self._pending_sockets: set[socket.socket] = set()
        self._active_socket: socket.socket | None = None
        self._connection_id: str | None = None
        self._connection_key: bytearray | None = None
        self._sequence = 0
        self._bound_port: int | None = None
        self._last_rejection: CommandError | None = None
        self._closed = False

    @property
    def port(self) -> int:
        with self._state_lock:
            if self._bound_port is None:
                raise RuntimeError("Runtime Host listener has not started.")
            return self._bound_port

    @property
    def authenticated(self) -> bool:
        with self._state_lock:
            return self._active_socket is not None and self._connection_id is not None

    def connection_state(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "transportAuthenticated": self._active_socket is not None,
                "sessionId": self._expected_session_id if self._active_socket is not None else None,
                "launchId": self._expected_launch_id if self._active_socket is not None else None,
                "expectedRuntimeRevision": self._expected_runtime_revision,
                "nativeCapabilitiesVerified": False,
            }

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Runtime Host transport is closed.")
            if self._listener is not None:
                raise RuntimeError("Runtime Host listener has already started.")
            family = socket.AF_INET6 if ":" in self._listen_address else socket.AF_INET
            listener = socket.socket(family, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._listen_address, self._requested_port))
            listener.listen(8)
            listener.settimeout(0.25)
            self._listener = listener
            self._bound_port = int(listener.getsockname()[1])
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                name="RelayLiveLoopHostAccept",
                daemon=True,
            )
            self._accept_thread.start()

    def wait_until_connected(self, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._active_socket is None:
                if self._last_rejection is not None:
                    error = self._last_rejection
                    self._last_rejection = None
                    raise error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError(
                        "CAPABILITY_UNAVAILABLE",
                        "No authenticated Player connection is available.",
                        stage="runtime_transport_accept",
                        runtime_changed=False,
                        recoverable=True,
                    )
                self._condition.wait(min(remaining, 0.1))

    def update_expected_runtime_revision(self, previous: str, current: str) -> None:
        self._require_canonical(previous, "previous")
        self._require_canonical(current, "current")
        with self._state_lock:
            if self._expected_runtime_revision != previous:
                raise CommandError(
                    "INPUT_CHANGED",
                    "Expected runtime revision changed before the Host could update it.",
                    stage="runtime_transport_revision",
                    runtime_changed=False,
                    recoverable=True,
                    details={"actualRuntimeRevision": self._expected_runtime_revision},
                )
            self._expected_runtime_revision = current

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        """CommandProvider-compatible adapter; V still controls registry verification state."""
        if not isinstance(command, dict):
            raise CommandError(
                "INVALID_REQUEST",
                "Runtime command must be an object.",
                stage="runtime_transport_encode",
                runtime_changed=False,
                recoverable=False,
            )
        request_id = command.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            raise CommandError(
                "INVALID_REQUEST",
                "Runtime command requires requestId.",
                stage="runtime_transport_encode",
                runtime_changed=False,
                recoverable=False,
            )
        try:
            payload = json.dumps(
                command,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CommandError(
                "INVALID_REQUEST",
                "Runtime command is not finite JSON data.",
                stage="runtime_transport_encode",
                runtime_changed=False,
                recoverable=False,
            ) from exc
        reply = self.invoke(request_id=request_id, operation=operation, payload=payload)
        if reply.media_type != "application/json":
            raise CommandError(
                "CONTRACT_MISMATCH",
                "Runtime provider response must use application/json.",
                stage="runtime_transport_result",
                runtime_changed=reply.runtime_changed,
                recoverable=False,
                details={"mediaType": reply.media_type},
            )
        try:
            value = self._decode_json_object(reply.payload)
        except ValueError as exc:
            raise CommandError(
                "CONTRACT_MISMATCH",
                "Runtime provider response is not a unique-field JSON object.",
                stage="runtime_transport_result",
                runtime_changed=reply.runtime_changed,
                recoverable=False,
            ) from exc
        if "runtimeChanged" in value and value["runtimeChanged"] != reply.runtime_changed:
            raise CommandError(
                "CONTRACT_MISMATCH",
                "Provider payload runtimeChanged disagrees with the Player revision authority.",
                stage="runtime_transport_result",
                runtime_changed=reply.runtime_changed,
                recoverable=False,
                details={
                    "wireRuntimeChanged": reply.runtime_changed,
                    "payloadRuntimeChanged": value["runtimeChanged"],
                },
            )
        return value

    def invoke(
        self,
        *,
        request_id: str,
        operation: str,
        payload: bytes,
        timeout_seconds: float | None = None,
    ) -> RuntimeTransportReply:
        self._require_canonical(request_id, "request_id")
        self._require_canonical(operation, "operation")
        if not payload:
            raise CommandError(
                "INVALID_REQUEST",
                "Authenticated runtime payload must not be empty.",
                stage="runtime_transport_encode",
                runtime_changed=False,
                recoverable=False,
            )
        timeout = self._command_timeout if timeout_seconds is None else timeout_seconds
        if not 0 < timeout <= 300:
            raise ValueError("timeout_seconds must be greater than zero and no more than 300.")

        with self._command_lock:
            with self._state_lock:
                active = self._active_socket
                connection_id = self._connection_id
                key = bytes(self._connection_key) if self._connection_key is not None else None
                expected_revision = self._expected_runtime_revision
                if active is None or connection_id is None or key is None:
                    raise CommandError(
                        "CAPABILITY_UNAVAILABLE",
                        "No authenticated Player connection is available.",
                        stage="runtime_transport_connectivity",
                        runtime_changed=False,
                        recoverable=True,
                    )
                self._sequence += 1
                sequence = self._sequence

            sent_at = int(time.time() * 1000)
            payload_sha256 = hashlib.sha256(payload).hexdigest()
            canonical = "\n".join(
                (
                    "RelayLiveLoop/request/1",
                    connection_id,
                    str(sequence),
                    str(sent_at),
                    request_id,
                    operation,
                    payload_sha256,
                )
            ).encode("utf-8")
            proof = base64.b64encode(hmac.new(key, canonical, hashlib.sha256).digest()).decode("ascii")
            message = {
                "kind": "request",
                "sessionId": self._expected_session_id,
                "expectedRuntimeRevision": expected_revision,
                "connectionId": connection_id,
                "sequence": sequence,
                "sentAtUnixMilliseconds": sent_at,
                "requestId": request_id,
                "operation": operation,
                "payloadSha256": payload_sha256,
                "proof": proof,
                "payloadBase64": base64.b64encode(payload).decode("ascii"),
            }
            active.settimeout(timeout)
            dispatch_may_have_started = False
            try:
                self._send_message(active, message)
                dispatch_may_have_started = True
                response = self._receive_message(active)
            except (OSError, EOFError, TimeoutError, ValueError) as exc:
                self._discard_session(active)
                if dispatch_may_have_started:
                    raise CommandError(
                        "STATE_UNKNOWN",
                        "The Player connection was lost after the complete request frame was sent; the request was not replayed.",
                        stage="runtime_transport_wait",
                        runtime_changed=None,
                        recoverable=True,
                        details={
                            "requestId": request_id,
                            "dispatchMayHaveStarted": True,
                            "automaticReplayAllowed": False,
                        },
                    ) from exc
                raise CommandError(
                    "CAPABILITY_UNAVAILABLE",
                    "The Player connection failed before a complete request frame was sent.",
                    stage="runtime_transport_send",
                    runtime_changed=False,
                    recoverable=True,
                    details={"requestId": request_id},
                ) from exc

            if response.get("requestId") != request_id:
                self._discard_session(active)
                raise CommandError(
                    "STATE_UNKNOWN",
                    "The Player response requestId did not match the sent command.",
                    stage="runtime_transport_result",
                    runtime_changed=None,
                    recoverable=False,
                    details={
                        "expectedRequestId": request_id,
                        "actualRequestId": response.get("requestId"),
                        "automaticReplayAllowed": False,
                    },
                )
            if response.get("kind") == "error":
                raise self._wire_error(response)
            if response.get("kind") != "response":
                self._discard_session(active)
                raise CommandError(
                    "STATE_UNKNOWN",
                    "The Player returned an invalid terminal message after dispatch.",
                    stage="runtime_transport_result",
                    runtime_changed=None,
                    recoverable=False,
                    details={"requestId": request_id, "automaticReplayAllowed": False},
                )

            try:
                runtime_changed_known = self._required_bool(response, "runtimeChangedKnown")
                if not runtime_changed_known:
                    raise ValueError("A successful response must have known runtimeChanged truth.")
                runtime_changed = self._required_bool(response, "runtimeChanged")
                schema_id = self._required_string(response, "schemaId")
                media_type = self._required_string(response, "mediaType")
                runtime_revision = self._required_string(response, "runtimeRevision")
                schema_version = response.get("schemaVersion")
                if type(schema_version) is not int or schema_version <= 0:
                    raise ValueError("schemaVersion must be a positive integer.")
                result_payload = self._decode_canonical_base64(response.get("payloadBase64"))
            except ValueError as exc:
                self._discard_session(active)
                known_change = response.get("runtimeChanged") if response.get("runtimeChangedKnown") is True else None
                raise self._contract_mismatch(
                    "Player terminal response fields do not match the runtime transport contract.",
                    known_change if type(known_change) is bool else None,
                ) from exc
            return RuntimeTransportReply(
                request_id=request_id,
                schema_id=schema_id,
                schema_version=schema_version,
                media_type=media_type,
                payload=result_payload,
                runtime_changed=runtime_changed,
                runtime_revision_after=runtime_revision,
            )

    def close(self) -> None:
        accept_thread: threading.Thread | None
        handshake_threads: list[threading.Thread]
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            listener = self._listener
            self._listener = None
            accept_thread = self._accept_thread
            active = self._active_socket
            self._clear_active_locked()
            pending = list(self._pending_sockets)
            self._pending_sockets.clear()
            handshake_threads = list(self._handshake_threads)
        if listener is not None:
            listener.close()
        if active is not None:
            self._close_socket(active)
        for pending_socket in pending:
            self._close_socket(pending_socket)
        deadline = time.monotonic() + self._shutdown_timeout
        for thread in ([accept_thread] if accept_thread is not None else []) + handshake_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in handshake_threads) or (
            accept_thread is not None and accept_thread.is_alive()
        ):
            raise TimeoutError("Runtime Host listener did not stop within the configured shutdown timeout.")
        for index in range(len(self._shared_secret)):
            self._shared_secret[index] = 0

    def __enter__(self) -> LoopbackRuntimeHostTransport:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            with self._state_lock:
                listener = self._listener
            if listener is None:
                return
            try:
                accepted, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                continue
            try:
                if not ipaddress.ip_address(peer[0].split("%")[0]).is_loopback:
                    accepted.close()
                    continue
            except ValueError:
                accepted.close()
                continue
            thread = threading.Thread(
                target=self._authenticate_accepted,
                args=(accepted,),
                name="RelayLiveLoopHostHandshake",
                daemon=True,
            )
            with self._state_lock:
                if self._closed or self._stopping.is_set():
                    accepted.close()
                    return
                self._pending_sockets.add(accepted)
                self._handshake_threads.add(thread)
            thread.start()

    def _authenticate_accepted(self, accepted: socket.socket) -> None:
        keep_socket = False
        try:
            accepted.settimeout(self._handshake_timeout)
            hello = self._receive_message(accepted)
            if hello.get("kind") != "player.hello":
                raise CommandError(
                    "INVALID_REQUEST",
                    "Accepted Player connection did not begin with player.hello.",
                    stage="runtime_transport_handshake",
                    runtime_changed=False,
                    recoverable=False,
                )
            client_nonce = self._random_token(24)
            self._send_message(
                accepted,
                {
                    "kind": "handshake.begin",
                    "protocolVersion": self._protocol_version,
                    "sessionId": self._expected_session_id,
                    "launchId": self._expected_launch_id,
                    "expectedRuntimeRevision": self._expected_runtime_revision,
                    "clientNonce": client_nonce,
                },
            )
            challenge = self._receive_message(accepted)
            if challenge.get("kind") == "error":
                raise self._wire_error(challenge)
            if challenge.get("kind") != "handshake.challenge":
                raise CommandError(
                    "CONTRACT_MISMATCH",
                    "Player returned an invalid handshake challenge.",
                    stage="runtime_transport_handshake",
                    runtime_changed=False,
                    recoverable=False,
                )
            if challenge.get("clientNonce") != client_nonce:
                raise CommandError(
                    "AUTH_REQUIRED",
                    "Handshake challenge client nonce did not match.",
                    stage="runtime_transport_handshake",
                    runtime_changed=False,
                    recoverable=False,
                )
            challenge_id = self._required_string(challenge, "challengeId")
            server_nonce = self._required_string(challenge, "serverNonce")
            runtime_revision = self._required_string(challenge, "runtimeRevision")
            canonical = "\n".join(
                (
                    "RelayLiveLoop/1",
                    str(self._protocol_version),
                    self._expected_session_id,
                    self._expected_launch_id,
                    runtime_revision,
                    challenge_id,
                    client_nonce,
                    server_nonce,
                )
            ).encode("utf-8")
            secret = bytes(self._shared_secret)
            proof = base64.b64encode(hmac.new(secret, canonical, hashlib.sha256).digest()).decode("ascii")
            connection_key = hmac.new(secret, b"connection\n" + canonical, hashlib.sha256).digest()
            self._send_message(
                accepted,
                {"kind": "handshake.complete", "challengeId": challenge_id, "proof": proof},
            )
            completed = self._receive_message(accepted)
            if completed.get("kind") == "error":
                raise self._wire_error(completed)
            if completed.get("kind") != "handshake.completed":
                raise CommandError(
                    "CONTRACT_MISMATCH",
                    "Player returned an invalid handshake completion.",
                    stage="runtime_transport_handshake",
                    runtime_changed=False,
                    recoverable=False,
                )
            connection_id = self._required_string(completed, "connectionId")
            with self._condition:
                if self._closed or self._stopping.is_set():
                    return
                if self._active_socket is not None:
                    raise CommandError(
                        "CONFLICT",
                        "An authenticated Player connection is already active.",
                        stage="runtime_transport_accept",
                        runtime_changed=False,
                        recoverable=True,
                    )
                if hello.get("sessionId") != self._expected_session_id or hello.get("launchId") != self._expected_launch_id:
                    raise CommandError(
                        "WRONG_SESSION",
                        "Player hello identity differs from the explicitly configured session.",
                        stage="runtime_transport_handshake",
                        runtime_changed=False,
                        recoverable=False,
                    )
                if hello.get("runtimeRevision") != self._expected_runtime_revision:
                    raise CommandError(
                        "STALE_TARGET",
                        "Player hello runtime revision differs from the explicitly configured revision.",
                        stage="runtime_transport_handshake",
                        runtime_changed=False,
                        recoverable=True,
                    )
                self._active_socket = accepted
                self._connection_id = connection_id
                self._connection_key = bytearray(connection_key)
                self._sequence = 0
                self._last_rejection = None
                keep_socket = True
                self._pending_sockets.discard(accepted)
                self._condition.notify_all()
        except CommandError as error:
            with self._condition:
                self._last_rejection = error
                self._condition.notify_all()
        except (OSError, EOFError, TimeoutError, ValueError) as exc:
            with self._condition:
                self._last_rejection = CommandError(
                    "AUTH_REQUIRED",
                    f"Player handshake did not complete ({type(exc).__name__}).",
                    stage="runtime_transport_handshake",
                    runtime_changed=False,
                    recoverable=True,
                )
                self._condition.notify_all()
        finally:
            if not keep_socket:
                self._close_socket(accepted)
            current = threading.current_thread()
            with self._state_lock:
                self._pending_sockets.discard(accepted)
                self._handshake_threads.discard(current)

    def _discard_session(self, active: socket.socket) -> None:
        with self._condition:
            if self._active_socket is active:
                self._clear_active_locked()
                self._condition.notify_all()
        self._close_socket(active)

    def _clear_active_locked(self) -> None:
        self._active_socket = None
        self._connection_id = None
        self._sequence = 0
        if self._connection_key is not None:
            for index in range(len(self._connection_key)):
                self._connection_key[index] = 0
        self._connection_key = None

    def _send_message(self, stream: socket.socket, value: dict[str, Any]) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not payload or len(payload) > self._maximum_frame_bytes:
            raise ValueError("Wire frame exceeds the configured message bound.")
        stream.sendall(struct.pack("!I", len(payload)) + payload)

    def _receive_message(self, stream: socket.socket) -> dict[str, Any]:
        header = self._receive_exact(stream, 4)
        (length,) = struct.unpack("!I", header)
        if length <= 0 or length > self._maximum_frame_bytes:
            raise ValueError("Wire frame length is outside the configured bound.")
        return self._decode_json_object(self._receive_exact(stream, length))

    @staticmethod
    def _receive_exact(stream: socket.socket, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = stream.recv(remaining)
            if not chunk:
                raise EOFError("Socket ended inside a framed message.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_json_object(payload: bytes) -> dict[str, Any]:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(f"Duplicate JSON field: {key}")
                value[key] = item
            return value

        try:
            decoded = payload.decode("utf-8", errors="strict")
            value = json.loads(decoded, object_pairs_hook=unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Wire payload is not strict UTF-8 JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Wire JSON root must be an object.")
        return value

    @staticmethod
    def _decode_canonical_base64(value: Any) -> bytes:
        if not isinstance(value, str) or not value:
            raise ValueError("Base64 value must be a non-empty string.")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Base64 value is malformed.") from exc
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("Base64 value is not canonical padded Base64.")
        return decoded

    @staticmethod
    def _random_token(byte_count: int) -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).decode("ascii").rstrip("=")

    @staticmethod
    def _require_canonical(value: str, label: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\n" in value or "\r" in value:
            raise ValueError(f"{label} must be a non-empty bounded canonical string.")

    @staticmethod
    def _required_string(value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise ValueError(f"{key} must be a non-empty string.")
        return item

    @staticmethod
    def _required_bool(value: dict[str, Any], key: str) -> bool:
        item = value.get(key)
        if type(item) is not bool:
            raise ValueError(f"{key} must be a boolean.")
        return item

    @staticmethod
    def _wire_error(message: dict[str, Any]) -> CommandError:
        error = message.get("error")
        if not isinstance(error, dict):
            return CommandError(
                "CONTRACT_MISMATCH",
                "Player error response omitted structured error details.",
                stage="runtime_transport_result",
                runtime_changed=None,
                recoverable=False,
            )
        details_value = error.get("details", [])
        details: dict[str, str] = {}
        if isinstance(details_value, list):
            for item in details_value:
                if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("value"), str):
                    details[item["key"]] = item["value"]
        known = error.get("runtimeChangedKnown")
        changed = error.get("runtimeChanged")
        runtime_changed = changed if type(known) is bool and known and type(changed) is bool else None
        code = error.get("code") if isinstance(error.get("code"), str) else "CONTRACT_MISMATCH"
        stage = error.get("stage") if isinstance(error.get("stage"), str) and error.get("stage") else "runtime_transport_result"
        text = error.get("message") if isinstance(error.get("message"), str) and error.get("message") else "Player returned an invalid error."
        recoverable = error.get("recoverable") if type(error.get("recoverable")) is bool else False
        return CommandError(
            code,
            text,
            stage=stage,
            runtime_changed=runtime_changed,
            recoverable=recoverable,
            details=details,
        )

    @staticmethod
    def _contract_mismatch(message: str, runtime_changed: bool | None) -> CommandError:
        return CommandError(
            "CONTRACT_MISMATCH",
            message,
            stage="runtime_transport_result",
            runtime_changed=runtime_changed,
            recoverable=False,
        )

    @staticmethod
    def _close_socket(value: socket.socket) -> None:
        try:
            value.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        value.close()
