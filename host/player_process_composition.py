from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.wintypes as wintypes
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactStore
from .ledger import Ledger
from .player_process_provider import (
    AuthorizationDecision,
    JsonPlayerStateStore,
    PlayerLaunchSpec,
    PlayerProcessProvider,
    PlayerSessionRecord,
    ProcessIdentity,
    ProcessObservation,
    ProcessStopResult,
    SESSION_FILE_ENV,
)


MACHINE_CONFIG_KIND = "relay-liveloop-machine"
MACHINE_CONFIG_VERSION = 1
MAX_MACHINE_CONFIG_BYTES = 256 * 1024
MAX_CATALOG_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 16 * 1024
MAX_REGISTRATION_BYTES = 16 * 1024
_IDENTIFIER_RE = re.compile(r"^[^\x00\r\n]{1,256}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_json_object(path: Path, maximum_bytes: int, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} must refer to an existing regular file.")
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"{label} exceeds its bounded size.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object.")
    return value


def _required_text(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{label} must be a bounded canonical text value.")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    text = _required_text(value, label, maximum=4096)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path.")
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} could not be resolved.") from exc


def _contained(parent: Path, candidate: Path) -> bool:
    try:
        parent_text = os.path.normcase(str(parent.resolve(strict=False)))
        candidate_text = os.path.normcase(str(candidate.resolve(strict=False)))
        return candidate_text == parent_text or candidate_text.startswith(parent_text.rstrip("\\/") + os.sep)
    except (OSError, RuntimeError):
        return False


@dataclass(frozen=True, slots=True)
class PlayerProcessMachineConfig:
    config_path: Path
    data_root: Path
    state_path: Path
    baseline_catalog_path: Path
    registration_directory: Path
    runtime_session_file: Path
    stop_timeout_seconds: float


def load_player_process_config(path: str | Path) -> PlayerProcessMachineConfig:
    """Load the small player-process extension of the existing machine config.

    The surrounding machine config keeps ownership of repository, data-root,
    token, and lifecycle policy.  This loader adds only paths for immutable
    baseline launch metadata, secret-free process registrations, provider
    state, and the existing protected runtime-session handoff.
    """

    config_path = Path(path).expanduser().resolve(strict=False)
    value = _read_json_object(config_path, MAX_MACHINE_CONFIG_BYTES, "--machine-config")
    if value.get("configKind") != MACHINE_CONFIG_KIND:
        raise ValueError("Machine configuration has an unsupported configKind.")
    if value.get("schemaVersion") != MACHINE_CONFIG_VERSION:
        raise ValueError("Machine configuration has an unsupported schemaVersion.")
    data_root = _absolute_path(value.get("dataRoot"), "dataRoot")
    if not data_root.is_dir():
        raise ValueError("dataRoot must refer to an existing directory.")
    player = value.get("playerProcess")
    if not isinstance(player, dict):
        raise ValueError("machine configuration requires a playerProcess object.")
    allowed = {
        "statePath",
        "baselineCatalogPath",
        "registrationDirectory",
        "runtimeSessionFile",
        "stopTimeoutSeconds",
    }
    if set(player) - allowed:
        raise ValueError("playerProcess contains unsupported fields.")

    state_path = _absolute_path(
        player.get("statePath", str(data_root / "player-process" / "state.json")),
        "playerProcess.statePath",
    )
    catalog_path = _absolute_path(player.get("baselineCatalogPath"), "playerProcess.baselineCatalogPath")
    registration_directory = _absolute_path(
        player.get("registrationDirectory"),
        "playerProcess.registrationDirectory",
    )
    runtime_session_file = _absolute_path(
        player.get("runtimeSessionFile"),
        "playerProcess.runtimeSessionFile",
    )
    for label, candidate in (
        ("playerProcess.statePath", state_path),
        ("playerProcess.baselineCatalogPath", catalog_path),
        ("playerProcess.registrationDirectory", registration_directory),
        ("playerProcess.runtimeSessionFile", runtime_session_file),
    ):
        if not _contained(data_root, candidate):
            raise ValueError(f"{label} must remain under dataRoot.")
    if not catalog_path.is_file():
        raise ValueError("playerProcess.baselineCatalogPath must refer to an existing regular file.")
    if not runtime_session_file.is_file():
        raise ValueError("playerProcess.runtimeSessionFile must refer to an existing regular file.")
    timeout = player.get("stopTimeoutSeconds", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 300:
        raise ValueError("playerProcess.stopTimeoutSeconds must be greater than zero and no more than 300.")
    registration_directory.mkdir(parents=True, exist_ok=True)
    return PlayerProcessMachineConfig(
        config_path=config_path,
        data_root=data_root,
        state_path=state_path,
        baseline_catalog_path=catalog_path,
        registration_directory=registration_directory,
        runtime_session_file=runtime_session_file,
        stop_timeout_seconds=float(timeout),
    )


@dataclass(frozen=True, slots=True)
class RuntimeSessionMetadata:
    session_id: str
    launch_id: str
    runtime_revision: str
    protocol_version: int
    host_address: str
    port: int


def load_runtime_session_metadata(path: str | Path) -> RuntimeSessionMetadata:
    """Read runtime-session identity without retaining or returning its secret."""

    candidate = Path(path).expanduser().resolve(strict=False)
    value = _read_json_object(candidate, MAX_SESSION_BYTES, "runtime session file")
    encoded = value.get("sharedSecretBase64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("runtime session file requires sharedSecretBase64.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("runtime session sharedSecretBase64 is not strict Base64.") from exc
    try:
        if len(decoded) < 32:
            raise ValueError("runtime session sharedSecretBase64 is shorter than 32 bytes.")
    finally:
        decoded = b""

    host_address = value.get("hostAddress", "127.0.0.1")
    try:
        address = ipaddress.ip_address(host_address)
    except ValueError as exc:
        raise ValueError("runtime session hostAddress must be a numeric loopback address.") from exc
    if not address.is_loopback:
        raise ValueError("runtime session hostAddress must be a numeric loopback address.")
    port = value.get("port", 18761)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("runtime session port is invalid.")
    protocol_version = value.get("protocolVersion", 1)
    if type(protocol_version) is not int or protocol_version <= 0:
        raise ValueError("runtime session protocolVersion is invalid.")
    return RuntimeSessionMetadata(
        _required_text(value.get("sessionId"), "runtime session sessionId"),
        _required_text(value.get("launchId"), "runtime session launchId"),
        _required_text(value.get("runtimeRevision"), "runtime session runtimeRevision"),
        protocol_version,
        str(address),
        port,
    )


def _catalog_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ValueError("baseline catalog must refer to an existing regular file.")
    raw = path.read_bytes()
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("baseline catalog exceeds its bounded size.")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("baseline catalog is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("baselines"), dict):
        raise ValueError("baseline catalog requires version 1 and a baselines object.")
    return value, digest


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(str(right.resolve(strict=False)))
    except (OSError, RuntimeError):
        return False


class LedgerBaselineResolver:
    """Resolve an imported Ledger artifact into a checked launch specification."""

    def __init__(self, ledger: Ledger, artifacts: ArtifactStore, config: PlayerProcessMachineConfig) -> None:
        self.ledger = ledger
        self.artifacts = artifacts
        self.config = config
        self._catalog, self._catalog_hash = _catalog_snapshot(config.baseline_catalog_path)
        self._session_metadata = load_runtime_session_metadata(config.runtime_session_file)

    def preflight(self) -> None:
        current, digest = _catalog_snapshot(self.config.baseline_catalog_path)
        if digest != self._catalog_hash:
            raise ValueError("baseline catalog changed after composition.")
        if current != self._catalog:
            raise ValueError("baseline catalog changed after composition.")
        for baseline_id in sorted(self._catalog["baselines"]):
            self._resolve_entry(baseline_id, self._catalog["baselines"][baseline_id])

    def resolve(self, baseline_id: str) -> PlayerLaunchSpec | None:
        current, digest = _catalog_snapshot(self.config.baseline_catalog_path)
        if digest != self._catalog_hash or current != self._catalog:
            raise ValueError("baseline catalog changed after composition.")
        entry = current["baselines"].get(baseline_id)
        if entry is None:
            return None
        return self._resolve_entry(baseline_id, entry)

    def _resolve_entry(self, baseline_id: str, entry: Any) -> PlayerLaunchSpec:
        if not isinstance(baseline_id, str) or not _IDENTIFIER_RE.fullmatch(baseline_id):
            raise ValueError("baseline catalog key is invalid.")
        if not isinstance(entry, dict):
            raise ValueError("baseline catalog entry must be an object.")
        allowed = {
            "artifactId",
            "executablePath",
            "arguments",
            "workingDirectory",
            "sessionFilePath",
            "sessionId",
            "launchId",
            "environment",
        }
        if set(entry) - allowed:
            raise ValueError("baseline catalog entry contains unsupported fields.")
        artifact_id = _required_text(entry.get("artifactId"), "baseline catalog artifactId")
        if artifact_id != baseline_id:
            raise ValueError("baseline catalog artifactId must equal its baselineId key.")
        artifact = self.ledger.get_artifact(artifact_id)
        metadata, stream = self.artifacts.open_verified(artifact_id)
        stream.close()
        if metadata["sha256"] != artifact["sha256"]:
            raise ValueError("Ledger artifact metadata changed during baseline resolution.")

        executable_path = _absolute_path(entry.get("executablePath"), "baseline catalog executablePath")
        artifact_path = Path(artifact["absolutePath"]).resolve(strict=False)
        if not executable_path.is_file() or not _same_path(executable_path, artifact_path):
            raise ValueError("baseline executablePath must be the verified imported artifact path.")
        arguments = entry.get("arguments", [])
        if not isinstance(arguments, list):
            raise ValueError("baseline catalog arguments must be an array.")
        normalized_arguments: list[str] = []
        for argument in arguments:
            normalized_arguments.append(_required_text(argument, "baseline catalog argument", maximum=4096))
        working_directory = _absolute_path(
            entry.get("workingDirectory", str(executable_path.parent)),
            "baseline catalog workingDirectory",
        )
        if not working_directory.is_dir():
            raise ValueError("baseline catalog workingDirectory must be an existing directory.")
        session_file_path = _absolute_path(
            entry.get("sessionFilePath", str(self.config.runtime_session_file)),
            "baseline catalog sessionFilePath",
        )
        if not _same_path(session_file_path, self.config.runtime_session_file) or not session_file_path.is_file():
            raise ValueError("baseline catalog sessionFilePath must be the configured runtime session file.")
        session_id = entry.get("sessionId", self._session_metadata.session_id)
        launch_id = entry.get("launchId", self._session_metadata.launch_id)
        if _required_text(session_id, "baseline catalog sessionId") != self._session_metadata.session_id:
            raise ValueError("baseline catalog sessionId differs from the protected runtime session.")
        if _required_text(launch_id, "baseline catalog launchId") != self._session_metadata.launch_id:
            raise ValueError("baseline catalog launchId differs from the protected runtime session.")
        environment = entry.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError("baseline catalog environment must be an object.")
        normalized_environment: dict[str, str] = {}
        for key, value in environment.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key) or key == SESSION_FILE_ENV:
                raise ValueError("baseline catalog environment contains a reserved or invalid name.")
            normalized_environment[key] = _required_text(value, "baseline catalog environment value", maximum=32768)
        return PlayerLaunchSpec(
            baseline_id=baseline_id,
            session_id=self._session_metadata.session_id,
            launch_id=self._session_metadata.launch_id,
            executable_path=str(executable_path),
            arguments=tuple(normalized_arguments),
            working_directory=str(working_directory),
            session_file_path=str(session_file_path),
            environment=normalized_environment,
        )


class LedgerAuthorizationVerifier:
    """Authorize player mutations from the existing durable approval records."""

    def __init__(self, ledger: Ledger) -> None:
        self._database_path = Path(ledger.path).resolve(strict=False)

    def preflight(self) -> None:
        with closing(self._read_connection()) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('approvals', 'plans')"
            ).fetchall()
        if {item[0] for item in row} != {"approvals", "plans"}:
            raise ValueError("Ledger approval tables are unavailable.")

    def authorize(
        self,
        operation: str,
        authorization_ref: str,
        *,
        baseline_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationDecision:
        if not isinstance(authorization_ref, str) or not authorization_ref:
            return AuthorizationDecision(False, "authorization_reference_missing")
        with closing(self._read_connection()) as connection:
            rows = connection.execute(
                """
                SELECT a.approval_id, a.user_confirmation_ref, a.approved_impact_json,
                       a.plan_id, p.approval_ref, p.prepare_complete, p.session_id,
                       p.details_json
                FROM approvals AS a
                JOIN plans AS p ON p.plan_id = a.plan_id
                WHERE a.approval_id = ? OR a.user_confirmation_ref = ?
                ORDER BY a.created_at DESC
                """,
                (authorization_ref, authorization_ref),
            ).fetchall()
        if len(rows) != 1:
            return AuthorizationDecision(False, "authorization_reference_missing_or_ambiguous")
        row = rows[0]
        if row["approval_ref"] != row["approval_id"] or not bool(row["prepare_complete"]):
            return AuthorizationDecision(False, "approval_is_not_currently_usable")
        try:
            impact = json.loads(row["approved_impact_json"])
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            return AuthorizationDecision(False, "approval_record_is_invalid")
        if not isinstance(impact, dict) or impact.get("restartPlayer") is not True:
            return AuthorizationDecision(False, "approval_does_not_cover_player_restart")
        if not isinstance(details, dict):
            return AuthorizationDecision(False, "approval_plan_details_are_invalid")
        if operation in {"player.start", "player.stop"}:
            if not isinstance(session_id, str) or not session_id or row["session_id"] != session_id:
                return AuthorizationDecision(False, "approval_session_mismatch")
        declared_baseline = details.get("baselineId")
        declared_baselines = details.get("baselineIds")
        if operation == "player.start":
            if not isinstance(baseline_id, str) or not baseline_id:
                return AuthorizationDecision(False, "approval_baseline_scope_required")
            has_baseline_scope = False
            if declared_baseline is not None:
                if not isinstance(declared_baseline, str) or declared_baseline != baseline_id:
                    return AuthorizationDecision(False, "approval_baseline_mismatch")
                has_baseline_scope = True
            if declared_baselines is not None:
                if not isinstance(declared_baselines, list) or any(not isinstance(item, str) for item in declared_baselines):
                    return AuthorizationDecision(False, "approval_baseline_scope_invalid")
                if baseline_id not in declared_baselines:
                    return AuthorizationDecision(False, "approval_baseline_mismatch")
                has_baseline_scope = True
            if not has_baseline_scope:
                return AuthorizationDecision(False, "approval_baseline_scope_required")
        elif baseline_id is not None:
            if not isinstance(declared_baseline, str) or declared_baseline != baseline_id:
                return AuthorizationDecision(False, "approval_baseline_mismatch")
            if declared_baselines is not None and (not isinstance(declared_baselines, list) or baseline_id not in declared_baselines):
                return AuthorizationDecision(False, "approval_baseline_mismatch")
        return AuthorizationDecision(True, "ledger_approval_accepted")

    def _read_connection(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise ValueError("Ledger database is unavailable.")
        try:
            connection = sqlite3.connect(self._database_path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ValueError("Ledger database could not be opened read-only.") from exc
        connection.row_factory = sqlite3.Row
        return connection


@dataclass(frozen=True, slots=True)
class AuthenticatedRuntimeSession:
    session_id: str
    launch_id: str
    runtime_revision: str


class AuthenticatedRuntimeSessionRegistry:
    """Read-only view over the existing authenticated runtime transport."""

    def __init__(self, transport: Any, metadata: RuntimeSessionMetadata) -> None:
        self._transport = transport
        self._metadata = metadata

    @property
    def authenticated(self) -> bool:
        return bool(self._transport is not None and getattr(self._transport, "authenticated", False))

    def current(self) -> AuthenticatedRuntimeSession | None:
        if not self.authenticated:
            return None
        state = self._connection_state()
        session_id = state.get("sessionId")
        launch_id = state.get("launchId")
        runtime_revision = state.get("expectedRuntimeRevision")
        if not all(isinstance(item, str) and item for item in (session_id, launch_id, runtime_revision)):
            return None
        if session_id != self._metadata.session_id or launch_id != self._metadata.launch_id or runtime_revision != self._metadata.runtime_revision:
            return None
        return AuthenticatedRuntimeSession(session_id, launch_id, runtime_revision)

    def matches(self, session_id: str, launch_id: str) -> bool:
        current = self.current()
        return current is not None and current.session_id == session_id and current.launch_id == launch_id

    def preflight(self) -> None:
        if not isinstance(self._metadata, RuntimeSessionMetadata):
            raise ValueError("runtime session metadata is unavailable.")
        if self._transport is not None and not callable(getattr(self._transport, "connection_state", None)):
            raise ValueError("runtime transport does not expose its authenticated session registry.")

    def _connection_state(self) -> dict[str, Any]:
        try:
            state = self._transport.connection_state()
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}


@dataclass(frozen=True, slots=True)
class RegisteredProcess:
    session_id: str
    launch_id: str
    identity: ProcessIdentity


class JsonPlayerRegistrationStore:
    """Secret-free process/session registration outside the source tree."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve(strict=False)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def read(self, session_id: str) -> RegisteredProcess | None:
        matches = [item for item in self._all() if item.session_id == session_id]
        if len(matches) > 1:
            raise ValueError("multiple process registrations exist for one session.")
        return matches[0] if matches else None

    def read_by_pid(self, process_id: int) -> RegisteredProcess | None:
        matches = [item for item in self._all() if item.identity.process_id == process_id]
        if len(matches) > 1:
            raise ValueError("multiple process registrations exist for one process.")
        return matches[0] if matches else None

    def write(self, session_id: str, launch_id: str, identity: ProcessIdentity) -> None:
        if not _IDENTIFIER_RE.fullmatch(session_id) or not _IDENTIFIER_RE.fullmatch(launch_id):
            raise ValueError("process registration session identity is invalid.")
        payload = {
            "version": 1,
            "sessionId": session_id,
            "launchId": launch_id,
            "identity": identity.as_dict(),
        }
        target = self.directory / ("registration-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
        with self._lock:
            descriptor, temporary = tempfile.mkstemp(prefix=".registration-", dir=str(self.directory))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def remove(self, record: PlayerSessionRecord) -> None:
        if record.identity is None:
            return
        target = self.directory / ("registration-" + hashlib.sha256(record.session_id.encode("utf-8")).hexdigest() + ".json")
        with self._lock:
            if not target.is_file():
                return
            current = self.read(record.session_id)
            if current is None or current.identity != record.identity or current.launch_id != record.launch_id:
                return
            target.unlink()

    def _all(self) -> list[RegisteredProcess]:
        with self._lock:
            result: list[RegisteredProcess] = []
            for path in sorted(self.directory.glob("*.json")):
                if path.stat().st_size > MAX_REGISTRATION_BYTES:
                    raise ValueError("process registration exceeds its bounded size.")
                value = _read_json_object(path, MAX_REGISTRATION_BYTES, "process registration")
                if value.get("version") != 1:
                    raise ValueError("process registration version is unsupported.")
                session_id = _required_text(value.get("sessionId"), "process registration sessionId")
                launch_id = _required_text(value.get("launchId"), "process registration launchId")
                identity_value = value.get("identity")
                if not isinstance(identity_value, dict) or set(identity_value) != {"processId", "startTimeUtc", "executablePath"}:
                    raise ValueError("process registration identity is invalid.")
                process_id = identity_value.get("processId")
                if type(process_id) is not int or process_id <= 0:
                    raise ValueError("process registration processId is invalid.")
                identity = ProcessIdentity(
                    process_id,
                    _required_text(identity_value.get("startTimeUtc"), "process registration startTimeUtc"),
                    _required_text(identity_value.get("executablePath"), "process registration executablePath", maximum=4096),
                )
                result.append(RegisteredProcess(session_id, launch_id, identity))
            return result


class WindowsProcessIdentityReader:
    """Concrete Windows PID, creation-time, and executable-path reader."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000

    def __init__(self) -> None:
        self._kernel32 = None
        if os.name == "nt":
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._kernel32.OpenProcess.restype = wintypes.HANDLE
            self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            self._kernel32.GetProcessTimes.restype = wintypes.BOOL
            self._kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    @property
    def available(self) -> bool:
        return self._kernel32 is not None

    def read(self, process_id: int) -> ProcessIdentity | None:
        if not self.available or type(process_id) is not int or process_id <= 0:
            return None
        access = self._PROCESS_QUERY_LIMITED_INFORMATION | self._SYNCHRONIZE
        handle = self._kernel32.OpenProcess(access, False, process_id)
        if not handle:
            return None
        try:
            return self.read_handle(handle, process_id)
        finally:
            self._kernel32.CloseHandle(handle)

    def read_handle(self, handle: Any, process_id: int) -> ProcessIdentity | None:
        if not self.available or not handle or type(process_id) is not int or process_id <= 0:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not self._kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        length = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(length.value)
        if not self._kernel32.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(length)):
            return None
        ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        unix_ticks = ticks - 116444736000000000
        start_time = datetime.fromtimestamp(unix_ticks / 10_000_000, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return ProcessIdentity(process_id, start_time, executable.value[: length.value])


class WindowsProcessHandle:
    def __init__(self, process_id: int, *, native_handle: Any = None, popen: subprocess.Popen[Any] | None = None) -> None:
        self.pid = process_id
        self.native_handle = native_handle
        self.popen = popen


class WindowsProcessAdapter:
    """Owned-start and identity-checked stop adapter for Windows processes."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258

    def __init__(self, registrations: JsonPlayerRegistrationStore, runtime_sessions: AuthenticatedRuntimeSessionRegistry) -> None:
        self._registrations = registrations
        self._runtime_sessions = runtime_sessions
        self._identity_reader = WindowsProcessIdentityReader()
        self._owned: dict[int, subprocess.Popen[Any]] = {}
        self._pending_sessions: dict[int, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._kernel32 = self._identity_reader._kernel32
        if self._kernel32 is not None:
            self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._kernel32.OpenProcess.restype = wintypes.HANDLE
            self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self._kernel32.TerminateProcess.restype = wintypes.BOOL
            self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self._kernel32.WaitForSingleObject.restype = wintypes.DWORD

    def preflight(self) -> None:
        if not self._identity_reader.available or self._kernel32 is None:
            raise ValueError("Windows process identity APIs are unavailable.")

    def locate(self, session_id: str) -> ProcessObservation:
        try:
            registration = self._registrations.read(session_id)
        except Exception:
            return ProcessObservation.unknown("process_registration_read_failed")
        if registration is None:
            return ProcessObservation.not_found("no_registered_process")
        if not self._runtime_sessions.authenticated:
            return ProcessObservation.unknown("runtime_session_not_authenticated")
        if not self._runtime_sessions.matches(registration.session_id, registration.launch_id):
            return ProcessObservation.unknown("runtime_session_identity_mismatch")
        handle = self.attach(registration.identity.process_id)
        if handle is None:
            return ProcessObservation.unknown("registered_process_could_not_be_opened")
        observation = self.inspect(handle)
        if observation.status != "running" or observation.identity is None:
            return observation
        if observation.identity != registration.identity or observation.session_id != registration.session_id or observation.launch_id != registration.launch_id:
            return ProcessObservation.unknown("process_registration_identity_mismatch")
        return observation

    def attach(self, process_id: int) -> WindowsProcessHandle | None:
        if not self._identity_reader.available or type(process_id) is not int or process_id <= 0:
            return None
        with self._lock:
            owned = self._owned.get(process_id)
            if owned is not None:
                if owned.poll() is None:
                    return WindowsProcessHandle(process_id, popen=owned)
                self._owned.pop(process_id, None)
            access = self._PROCESS_QUERY_LIMITED_INFORMATION | self._PROCESS_TERMINATE | self._SYNCHRONIZE
            native_handle = self._kernel32.OpenProcess(access, False, process_id)
            if not native_handle:
                return None
            return WindowsProcessHandle(process_id, native_handle=native_handle)

    def start(
        self,
        executable_path: str,
        arguments: tuple[str, ...] | list[str],
        working_directory: str,
        environment: Mapping[str, str],
    ) -> WindowsProcessHandle:
        self.preflight()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child = subprocess.Popen(
            [executable_path, *arguments],
            cwd=working_directory,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        with self._lock:
            self._owned[child.pid] = child
        return WindowsProcessHandle(child.pid, popen=child)

    def prepare_started_session(self, process: WindowsProcessHandle, session_id: str, launch_id: str) -> None:
        if not isinstance(process, WindowsProcessHandle) or process.popen is None:
            raise ValueError("started process handle is not provider-owned.")
        with self._lock:
            self._pending_sessions[process.pid] = (session_id, launch_id)

    def inspect(self, process: WindowsProcessHandle) -> ProcessObservation:
        if not isinstance(process, WindowsProcessHandle):
            return ProcessObservation.unknown("process_handle_type_invalid")
        if process.popen is not None:
            try:
                if process.popen.poll() is not None:
                    return ProcessObservation.not_found("process_exited")
            except Exception:
                return ProcessObservation.unknown("process_poll_failed")
        else:
            exit_code = wintypes.DWORD()
            if not self._kernel32.GetExitCodeProcess(process.native_handle, ctypes.byref(exit_code)):
                return ProcessObservation.unknown("process_exit_code_read_failed")
            if exit_code.value != self._STILL_ACTIVE:
                return ProcessObservation.not_found("process_exited")
        if process.native_handle is not None:
            identity = self._identity_reader.read_handle(process.native_handle, process.pid)
        else:
            identity = self._identity_reader.read(process.pid)
        if identity is None:
            return ProcessObservation.unknown("process_identity_unavailable")
        session = self._session_for_process(process.pid)
        if session is None:
            return ProcessObservation.unknown("process_session_identity_unavailable")
        return ProcessObservation.running(process, identity, session_id=session[0], launch_id=session[1])

    def stop(self, process: WindowsProcessHandle, timeout_seconds: float) -> ProcessStopResult:
        if not isinstance(process, WindowsProcessHandle):
            return ProcessStopResult("unknown", reason="process_handle_type_invalid")
        timeout_ms = max(0, int(timeout_seconds * 1000))
        try:
            if process.popen is not None:
                process.popen.terminate()
                process.popen.wait(timeout=max(0.0, timeout_seconds))
                still_running = process.popen.poll() is None
            else:
                if not self._kernel32.TerminateProcess(process.native_handle, 0):
                    return ProcessStopResult("unknown", reason="stop_request_failed")
                wait_result = self._kernel32.WaitForSingleObject(process.native_handle, timeout_ms)
                if wait_result == self._WAIT_TIMEOUT:
                    return ProcessStopResult("still_running", reason="stop_timeout")
                if wait_result != self._WAIT_OBJECT_0:
                    return ProcessStopResult("unknown", reason="stop_wait_failed")
                still_running = False
        except subprocess.TimeoutExpired:
            return ProcessStopResult("still_running", reason="stop_timeout")
        except Exception:
            return ProcessStopResult("unknown", reason="stop_request_failed")
        finally:
            if process.popen is not None:
                with self._lock:
                    self._owned.pop(process.pid, None)
                    self._pending_sessions.pop(process.pid, None)
        return ProcessStopResult("still_running" if still_running else "stopped")

    def release(self, process: WindowsProcessHandle) -> None:
        if not isinstance(process, WindowsProcessHandle):
            return
        native_handle = process.native_handle
        process.native_handle = None
        if native_handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(native_handle)

    def record_owned_process(self, observation: ProcessObservation) -> None:
        if observation.status != "running" or observation.identity is None or not observation.session_id or not observation.launch_id:
            raise ValueError("owned process observation is incomplete.")
        self._registrations.write(observation.session_id, observation.launch_id, observation.identity)

    def forget_owned_process(self, record: PlayerSessionRecord) -> None:
        self._registrations.remove(record)

    def _session_for_process(self, process_id: int) -> tuple[str, str] | None:
        with self._lock:
            pending = self._pending_sessions.get(process_id)
        try:
            registration = self._registrations.read_by_pid(process_id)
        except Exception:
            return None
        if self._runtime_sessions.authenticated:
            current = self._runtime_sessions.current()
            if current is None:
                return None
            if pending is not None and not self._runtime_sessions.matches(*pending):
                return None
            if registration is not None and not self._runtime_sessions.matches(registration.session_id, registration.launch_id):
                return None
            if pending is not None:
                return pending
            if registration is not None:
                return registration.session_id, registration.launch_id
            return None
        return pending


class PlayerProcessCompositionEvidence:
    """Read-only configuration evidence used by ProviderRegistry.

    This deliberately does not consult ``authenticated`` and does not claim a
    Player, runtime, native, device, or product acceptance gate.  Runtime
    authentication remains a separate live fact.
    """

    def __init__(
        self,
        resolver: LedgerBaselineResolver,
        authorization: LedgerAuthorizationVerifier,
        runtime_sessions: AuthenticatedRuntimeSessionRegistry,
        adapter: WindowsProcessAdapter,
    ) -> None:
        self._resolver = resolver
        self._authorization = authorization
        self._runtime_sessions = runtime_sessions
        self._adapter = adapter

    def __call__(self) -> bool:
        try:
            self._resolver.preflight()
            self._authorization.preflight()
            self._runtime_sessions.preflight()
            self._adapter.preflight()
            return True
        except Exception:
            return False


def create_player_process_provider(
    *,
    machine_config_path: str | Path | None = None,
    config: PlayerProcessMachineConfig | None = None,
    ledger: Ledger,
    artifacts: ArtifactStore,
    runtime_transport: Any = None,
) -> PlayerProcessProvider:
    """Compose the production provider from existing Host facts and config."""

    effective_config = config
    if effective_config is None and machine_config_path is not None:
        effective_config = load_player_process_config(machine_config_path)
    if effective_config is None:
        raise ValueError("machine_config_path or config is required.")
    metadata = load_runtime_session_metadata(effective_config.runtime_session_file)
    runtime_sessions = AuthenticatedRuntimeSessionRegistry(runtime_transport, metadata)
    resolver = LedgerBaselineResolver(ledger, artifacts, effective_config)
    authorization = LedgerAuthorizationVerifier(ledger)
    registrations = JsonPlayerRegistrationStore(effective_config.registration_directory)
    adapter = WindowsProcessAdapter(registrations, runtime_sessions)
    evidence = PlayerProcessCompositionEvidence(resolver, authorization, runtime_sessions, adapter)
    provider = PlayerProcessProvider(
        resolver,
        adapter,
        authorization=authorization,
        state_store=JsonPlayerStateStore(effective_config.state_path),
        stop_timeout_seconds=effective_config.stop_timeout_seconds,
        verification=evidence,
    )
    provider.provider_id = "windows-player-process"
    return provider


__all__ = [
    "AuthenticatedRuntimeSession",
    "AuthenticatedRuntimeSessionRegistry",
    "JsonPlayerRegistrationStore",
    "LedgerAuthorizationVerifier",
    "LedgerBaselineResolver",
    "PlayerProcessCompositionEvidence",
    "PlayerProcessMachineConfig",
    "RegisteredProcess",
    "RuntimeSessionMetadata",
    "WindowsProcessAdapter",
    "WindowsProcessHandle",
    "WindowsProcessIdentityReader",
    "create_player_process_provider",
    "load_player_process_config",
    "load_runtime_session_metadata",
]
