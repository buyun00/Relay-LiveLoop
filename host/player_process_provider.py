from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
from uuid import uuid4

from host.errors import CommandError


CAPABILITY = "player_process"
SESSION_FILE_ENV = "RELAY_LIVELOOP_RUNTIME_SESSION_FILE"
_IDENTIFIER_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ProcessStatus = Literal["running", "not_found", "unknown"]
StopStatus = Literal["stopped", "still_running", "unknown"]


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """OS identity facts supplied by the process adapter, never inferred here."""

    process_id: int
    start_time_utc: str
    executable_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "processId": self.process_id,
            "startTimeUtc": self.start_time_utc,
            "executablePath": self.executable_path,
        }


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    """A three-valued process observation.

    ``not_found`` is a definite absence. ``unknown`` means that the adapter
    could not establish the process state and must never be treated as absent.
    The opaque process handle is kept out of serialized state and responses.
    """

    status: ProcessStatus
    process: Any = field(default=None, repr=False, compare=False)
    identity: ProcessIdentity | None = None
    session_id: str | None = None
    launch_id: str | None = None
    reason: str | None = None

    @classmethod
    def running(
        cls,
        process: Any,
        identity: ProcessIdentity,
        *,
        session_id: str | None,
        launch_id: str | None,
    ) -> "ProcessObservation":
        return cls("running", process, identity, session_id, launch_id)

    @classmethod
    def not_found(cls, reason: str = "process_not_found") -> "ProcessObservation":
        return cls("not_found", reason=reason)

    @classmethod
    def unknown(cls, reason: str) -> "ProcessObservation":
        return cls("unknown", reason=reason)


@dataclass(frozen=True, slots=True)
class ProcessStopResult:
    status: StopStatus
    exit_code: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str = "authorization_denied"


@dataclass(frozen=True, slots=True)
class PlayerLaunchSpec:
    """Neutral launch material resolved from an imported baseline.

    The protected session handoff is represented only by its path. The path is
    injected into ``session_file_env`` and is intentionally never added to the
    child argument vector. A resolver must not put secret values in any field.
    """

    baseline_id: str
    session_id: str
    launch_id: str
    executable_path: str
    arguments: tuple[str, ...]
    working_directory: str
    session_file_path: str
    environment: Mapping[str, str] = field(default_factory=dict)
    session_file_env: str = SESSION_FILE_ENV


@dataclass(slots=True)
class PlayerSessionRecord:
    session_id: str
    launch_id: str
    baseline_id: str | None
    identity: ProcessIdentity | None
    expected_executable_path: str | None
    owned_by_provider: bool | None
    ownership_id: str | None
    state: Literal["started", "attached", "unknown", "stopped"]
    stop_allowed: bool
    created_at_utc: str
    updated_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "launchId": self.launch_id,
            "baselineId": self.baseline_id,
            "identity": self.identity.as_dict() if self.identity is not None else None,
            "expectedExecutablePath": self.expected_executable_path,
            "ownedByProvider": self.owned_by_provider,
            "ownershipId": self.ownership_id,
            "state": self.state,
            "stopAllowed": self.stop_allowed,
            "createdAtUtc": self.created_at_utc,
            "updatedAtUtc": self.updated_at_utc,
        }


@dataclass(slots=True)
class PlayerStartIntent:
    baseline_id: str
    session_id: str
    launch_id: str
    expected_executable_path: str
    created_at_utc: str
    updated_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "baselineId": self.baseline_id,
            "sessionId": self.session_id,
            "launchId": self.launch_id,
            "expectedExecutablePath": self.expected_executable_path,
            "createdAtUtc": self.created_at_utc,
            "updatedAtUtc": self.updated_at_utc,
        }


@dataclass(slots=True)
class PlayerState:
    sessions: dict[str, PlayerSessionRecord] = field(default_factory=dict)
    blocked_baselines: dict[str, str] = field(default_factory=dict)
    start_intents: dict[str, PlayerStartIntent] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sessions": {key: value.as_dict() for key, value in sorted(self.sessions.items())},
            "blockedBaselines": dict(sorted(self.blocked_baselines.items())),
            "startIntents": {key: value.as_dict() for key, value in sorted(self.start_intents.items())},
        }


class BaselineResolver(Protocol):
    def resolve(self, baseline_id: str) -> PlayerLaunchSpec | None: ...


class AuthorizationVerifier(Protocol):
    def authorize(
        self,
        operation: str,
        authorization_ref: str,
        *,
        baseline_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationDecision: ...


class ProcessAdapter(Protocol):
    """The only boundary allowed to discover, start, inspect, or stop a process."""

    def locate(self, session_id: str) -> ProcessObservation: ...

    def attach(self, process_id: int) -> Any | None: ...

    def start(
        self,
        executable_path: str,
        arguments: Sequence[str],
        working_directory: str,
        environment: Mapping[str, str],
    ) -> Any: ...

    def inspect(self, process: Any) -> ProcessObservation: ...

    def stop(self, process: Any, timeout_seconds: float) -> ProcessStopResult: ...


class PlayerStateStore(Protocol):
    def read(self) -> PlayerState: ...

    def write(self, state: PlayerState) -> None: ...


class DenyAllAuthorization:
    """Safe default: wiring a provider does not implicitly grant process control."""

    def authorize(
        self,
        operation: str,
        authorization_ref: str,
        *,
        baseline_id: str | None = None,
        session_id: str | None = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(False, "no_authorization_verifier_configured")


class InMemoryPlayerStateStore:
    """Small store for composition tests and hosts that own a longer-lived store."""

    def __init__(self, state: PlayerState | None = None) -> None:
        self._state = copy.deepcopy(state) if state is not None else PlayerState()

    def read(self) -> PlayerState:
        return copy.deepcopy(self._state)

    def write(self, state: PlayerState) -> None:
        self._state = copy.deepcopy(state)


def _identity_from_dict(value: Any) -> ProcessIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("identity must be an object or null")
    if set(value) != {"processId", "startTimeUtc", "executablePath"}:
        raise ValueError("identity fields are invalid")
    process_id = value["processId"]
    if type(process_id) is not int or process_id <= 0:
        raise ValueError("identity.processId is invalid")
    if not isinstance(value["startTimeUtc"], str) or not value["startTimeUtc"]:
        raise ValueError("identity.startTimeUtc is invalid")
    if not isinstance(value["executablePath"], str) or not value["executablePath"]:
        raise ValueError("identity.executablePath is invalid")
    return ProcessIdentity(process_id, value["startTimeUtc"], value["executablePath"])


def _record_from_dict(value: Any) -> PlayerSessionRecord:
    if not isinstance(value, dict):
        raise ValueError("player session record must be an object")
    required = {
        "sessionId",
        "launchId",
        "baselineId",
        "identity",
        "expectedExecutablePath",
        "ownedByProvider",
        "ownershipId",
        "state",
        "stopAllowed",
        "createdAtUtc",
        "updatedAtUtc",
    }
    if set(value) != required:
        raise ValueError("player session record fields are invalid")
    if not isinstance(value["sessionId"], str) or not value["sessionId"]:
        raise ValueError("sessionId is invalid")
    if not isinstance(value["launchId"], str) or not value["launchId"]:
        raise ValueError("launchId is invalid")
    if value["baselineId"] is not None and not isinstance(value["baselineId"], str):
        raise ValueError("baselineId is invalid")
    owned = value["ownedByProvider"]
    if owned is not None and type(owned) is not bool:
        raise ValueError("ownedByProvider is invalid")
    if value["ownershipId"] is not None and not isinstance(value["ownershipId"], str):
        raise ValueError("ownershipId is invalid")
    state = value["state"]
    if state not in {"started", "attached", "unknown", "stopped"}:
        raise ValueError("player session state is invalid")
    if type(value["stopAllowed"]) is not bool:
        raise ValueError("stopAllowed is invalid")
    for key in ("createdAtUtc", "updatedAtUtc"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"{key} is invalid")
    expected = value["expectedExecutablePath"]
    if expected is not None and not isinstance(expected, str):
        raise ValueError("expectedExecutablePath is invalid")
    return PlayerSessionRecord(
        session_id=value["sessionId"],
        launch_id=value["launchId"],
        baseline_id=value["baselineId"],
        identity=_identity_from_dict(value["identity"]),
        expected_executable_path=expected,
        owned_by_provider=owned,
        ownership_id=value["ownershipId"],
        state=state,
        stop_allowed=value["stopAllowed"],
        created_at_utc=value["createdAtUtc"],
        updated_at_utc=value["updatedAtUtc"],
    )


def _start_intent_from_dict(value: Any) -> PlayerStartIntent:
    if not isinstance(value, dict):
        raise ValueError("player start intent must be an object")
    required = {
        "baselineId",
        "sessionId",
        "launchId",
        "expectedExecutablePath",
        "createdAtUtc",
        "updatedAtUtc",
    }
    if set(value) != required:
        raise ValueError("player start intent fields are invalid")
    for key in ("baselineId", "sessionId", "launchId", "expectedExecutablePath", "createdAtUtc", "updatedAtUtc"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"player start intent {key} is invalid")
    return PlayerStartIntent(
        baseline_id=value["baselineId"],
        session_id=value["sessionId"],
        launch_id=value["launchId"],
        expected_executable_path=value["expectedExecutablePath"],
        created_at_utc=value["createdAtUtc"],
        updated_at_utc=value["updatedAtUtc"],
    )


def _state_from_dict(value: Any) -> PlayerState:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("player state version is unsupported")
    sessions_value = value.get("sessions")
    blocked_value = value.get("blockedBaselines")
    intents_value = value.get("startIntents", {})
    if not isinstance(sessions_value, dict) or not isinstance(blocked_value, dict) or not isinstance(intents_value, dict):
        raise ValueError("player state fields are invalid")
    sessions = {key: _record_from_dict(record) for key, record in sessions_value.items()}
    blocked = {}
    for key, reason in blocked_value.items():
        if not isinstance(key, str) or not isinstance(reason, str):
            raise ValueError("blocked baseline record is invalid")
        blocked[key] = reason
    intents = {key: _start_intent_from_dict(intent) for key, intent in intents_value.items()}
    for key, intent in intents.items():
        if key != intent.baseline_id:
            raise ValueError("player start intent key does not match baselineId")
    return PlayerState(sessions=sessions, blocked_baselines=blocked, start_intents=intents)


class JsonPlayerStateStore:
    """Atomic, secret-free provider state for rebind after a Host restart."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    def read(self) -> PlayerState:
        with self._lock:
            state = PlayerState()
            if self.path.is_file():
                state = _state_from_dict(json.loads(self.path.read_text(encoding="utf-8")))
            marker = self._unresolved_marker_path()
            if marker.is_file():
                unresolved = _state_from_dict(json.loads(marker.read_text(encoding="utf-8")))
                state.sessions.update(unresolved.sessions)
                state.blocked_baselines.update(unresolved.blocked_baselines)
                state.start_intents.update(unresolved.start_intents)
            return state

    def write(self, state: PlayerState) -> None:
        payload = json.dumps(state.as_dict(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                try:
                    self._unresolved_marker_path().unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def write_unresolved_marker(self, state: PlayerState) -> None:
        """Persist a retry-blocking state when the primary state write fails."""
        payload = json.dumps(state.as_dict(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        marker = self._unresolved_marker_path()
        with self._lock:
            marker.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{marker.name}.", dir=str(marker.parent))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, marker)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _unresolved_marker_path(self) -> Path:
        return self.path.with_name(self.path.name + ".unresolved.json")


class SubprocessProcessAdapter:
    """A subprocess adapter with injected identity/session readers.

    Identity and session/launch discovery are deliberately injected because
    platform/runtime ownership proof is deployment-specific. The adapter uses
    an explicit environment and redirects child output so neither secrets nor
    command material are copied into Host logs. It never calls ``kill``.
    """

    def __init__(
        self,
        identity_reader: Callable[[int], ProcessIdentity | None],
        session_reader: Callable[[int], tuple[str, str] | None],
        *,
        locator: Callable[[str], int | Any | None] | None = None,
        attacher: Callable[[int], Any | None] | None = None,
    ) -> None:
        self._identity_reader = identity_reader
        self._session_reader = session_reader
        self._locator = locator
        self._attacher = attacher

    def locate(self, session_id: str) -> ProcessObservation:
        if self._locator is None:
            return ProcessObservation.unknown("process_locator_not_configured")
        try:
            located = self._locator(session_id)
        except Exception:
            return ProcessObservation.unknown("process_locator_failed")
        if located is None:
            return ProcessObservation.not_found()
        if isinstance(located, ProcessObservation):
            return located
        handle = self.attach(located) if type(located) is int else located
        if handle is None:
            return ProcessObservation.unknown("located_process_could_not_be_attached")
        return self.inspect(handle)

    def attach(self, process_id: int) -> Any | None:
        if self._attacher is None:
            return None
        return self._attacher(process_id)

    def start(
        self,
        executable_path: str,
        arguments: Sequence[str],
        working_directory: str,
        environment: Mapping[str, str],
    ) -> Any:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            [executable_path, *arguments],
            cwd=working_directory,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )

    def inspect(self, process: Any) -> ProcessObservation:
        process_id = getattr(process, "pid", None)
        if type(process_id) is not int or process_id <= 0:
            return ProcessObservation.unknown("process_handle_has_no_pid")
        poll = getattr(process, "poll", None)
        if callable(poll):
            try:
                if poll() is not None:
                    return ProcessObservation.not_found("process_exited")
            except Exception:
                return ProcessObservation.unknown("process_poll_failed")
        try:
            identity = self._identity_reader(process_id)
        except Exception:
            return ProcessObservation.unknown("process_identity_read_failed")
        if identity is None:
            return ProcessObservation.unknown("process_identity_unavailable")
        try:
            session = self._session_reader(process_id)
        except Exception:
            return ProcessObservation.unknown("process_session_identity_read_failed")
        if session is None:
            return ProcessObservation.unknown("process_session_identity_unavailable")
        session_id, launch_id = session
        return ProcessObservation.running(
            process,
            identity,
            session_id=session_id,
            launch_id=launch_id,
        )

    def stop(self, process: Any, timeout_seconds: float) -> ProcessStopResult:
        terminate = getattr(process, "terminate", None)
        wait = getattr(process, "wait", None)
        poll = getattr(process, "poll", None)
        if not callable(terminate) or not callable(wait):
            return ProcessStopResult("unknown", reason="process_handle_cannot_stop")
        try:
            terminate()
            wait(timeout=max(0.0, timeout_seconds))
        except subprocess.TimeoutExpired:
            return ProcessStopResult("still_running", reason="stop_timeout")
        except Exception:
            return ProcessStopResult("unknown", reason="stop_request_failed")
        try:
            still_running = callable(poll) and poll() is None
        except Exception:
            return ProcessStopResult("unknown", reason="post_stop_poll_failed")
        return ProcessStopResult("still_running" if still_running else "stopped")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except (OSError, TypeError):
        return False


def _error_result(
    code: str,
    message: str,
    *,
    runtime_changed: bool | None,
    recoverable: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = "state_unknown" if code == "STATE_UNKNOWN" else "failed"
    return {
        "status": status,
        "result": {},
        "error": {
            "code": code,
            "stage": CAPABILITY,
            "message": message,
            "recoverable": recoverable,
            "details": details or {},
        },
        "runtimeChanged": runtime_changed,
        "facts": {},
        "artifacts": [],
        "timingsMs": {},
    }


class PlayerProcessProvider:
    capability = CAPABILITY
    provider_id = "generic-player-process"

    def __init__(
        self,
        baseline_resolver: BaselineResolver,
        process_adapter: ProcessAdapter,
        *,
        authorization: AuthorizationVerifier | None = None,
        state_store: PlayerStateStore | None = None,
        stop_timeout_seconds: float = 10.0,
        verified: bool = False,
        verification: Callable[[], bool] | None = None,
    ) -> None:
        if not 0 < stop_timeout_seconds <= 300:
            raise ValueError("stop_timeout_seconds must be between 0 and 300")
        self.baseline_resolver = baseline_resolver
        self.process_adapter = process_adapter
        self.authorization = authorization or DenyAllAuthorization()
        self.state_store = state_store or InMemoryPlayerStateStore()
        self.stop_timeout_seconds = float(stop_timeout_seconds)
        self._static_verified = bool(verified)
        self._verification = verification
        self._volatile_blocked_baselines: set[str] = set()
        self._lock = threading.RLock()

    @property
    def is_verified(self) -> bool:
        """Report composition evidence, never runtime or product acceptance.

        The production factory supplies a read-only preflight callback instead
        of asserting this value at registration time.  The legacy ``verified``
        argument remains available for isolated adapter tests only.
        """
        if self._verification is None:
            return self._static_verified
        try:
            return bool(self._verification())
        except Exception:
            return False

    def execute(self, operation: str, command: dict[str, Any]) -> dict[str, Any]:
        if operation not in {"player.start", "player.attach", "player.stop"}:
            return _error_result(
                "CONTRACT_MISMATCH",
                "Player process provider does not support this operation.",
                runtime_changed=False,
            )
        arguments = command.get("arguments")
        if not isinstance(arguments, dict):
            return _error_result("CONTRACT_MISMATCH", "Player operation arguments must be an object.", runtime_changed=False)
        with self._lock:
            if operation == "player.start":
                return self._start(arguments)
            if operation == "player.attach":
                return self._attach(arguments)
            return self._stop(arguments)

    def _authorization_result(
        self,
        operation: str,
        authorization_ref: Any,
        *,
        baseline_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(authorization_ref, str) or not authorization_ref:
            return _error_result("AUTH_REQUIRED", "An explicit authorization reference is required.", runtime_changed=False)
        try:
            decision = self.authorization.authorize(
                operation,
                authorization_ref,
                baseline_id=baseline_id,
                session_id=session_id,
            )
        except Exception:
            return _error_result(
                "STATE_UNKNOWN",
                "Authorization could not be resolved; no process action was attempted.",
                runtime_changed=False,
                recoverable=False,
            )
        if not isinstance(decision, AuthorizationDecision):
            return _error_result(
                "STATE_UNKNOWN",
                "Authorization returned an unverifiable decision; no process action was attempted.",
                runtime_changed=False,
                recoverable=False,
            )
        if not decision.allowed:
            return _error_result("AUTH_REQUIRED", "The explicit process authorization was not accepted.", runtime_changed=False)
        return None

    @staticmethod
    def _validate_spec(spec: PlayerLaunchSpec, requested_baseline_id: str) -> dict[str, str] | None:
        if spec.baseline_id != requested_baseline_id:
            return {"reason": "baseline_resolver_identity_mismatch"}
        for key, value in (
            ("baseline_id", spec.baseline_id),
            ("session_id", spec.session_id),
            ("launch_id", spec.launch_id),
        ):
            if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
                return {"reason": f"invalid_{key}"}
        executable = Path(spec.executable_path)
        working_directory = Path(spec.working_directory)
        session_file = Path(spec.session_file_path)
        if not executable.is_absolute() or not working_directory.is_absolute() or not session_file.is_absolute():
            return {"reason": "launch_paths_must_be_absolute"}
        if not executable.is_file():
            return {"reason": "executable_path_is_not_a_file"}
        if not working_directory.is_dir():
            return {"reason": "working_directory_is_not_a_directory"}
        if not session_file.is_file():
            return {"reason": "session_file_path_is_not_a_file"}
        if not isinstance(spec.session_file_env, str) or not _ENV_NAME_RE.fullmatch(spec.session_file_env):
            return {"reason": "session_file_env_is_invalid"}
        if not isinstance(spec.arguments, tuple):
            return {"reason": "arguments_must_be_a_tuple"}
        for argument in spec.arguments:
            if not isinstance(argument, str) or "\x00" in argument or "\r" in argument or "\n" in argument:
                return {"reason": "arguments_contain_invalid_text"}
        if str(session_file) in spec.arguments:
            return {"reason": "session_file_path_must_not_be_a_command_argument"}
        if not isinstance(spec.environment, Mapping):
            return {"reason": "environment_must_be_a_mapping"}
        for key, value in spec.environment.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                return {"reason": "environment_contains_invalid_name"}
            if not isinstance(value, str) or "\x00" in value:
                return {"reason": "environment_contains_invalid_value"}
        if spec.session_file_env in spec.environment and spec.environment[spec.session_file_env] != str(session_file):
            return {"reason": "session_file_env_is_reserved"}
        return None

    def _start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        baseline_id = arguments.get("baselineId")
        authorization_ref = arguments.get("authorizationRef")
        if not isinstance(baseline_id, str) or not baseline_id:
            return _error_result("CONTRACT_MISMATCH", "baselineId is required.", runtime_changed=False)

        state = self.state_store.read()
        if baseline_id in state.blocked_baselines or baseline_id in state.start_intents or baseline_id in self._volatile_blocked_baselines:
            return _error_result(
                "STATE_UNKNOWN",
                "A prior start outcome is unresolved; reconcile the player identity before retrying.",
                runtime_changed=None,
                recoverable=False,
            )
        try:
            spec = self.baseline_resolver.resolve(baseline_id)
        except Exception:
            self._block_and_persist(state, baseline_id, "baseline_resolver_failed")
            return _error_result(
                "STATE_UNKNOWN",
                "The baseline could not be resolved; no process start was attempted.",
                runtime_changed=None,
                recoverable=False,
            )
        if spec is None:
            return _error_result("CONTRACT_MISMATCH", "baselineId is not registered.", runtime_changed=False)
        invalid_spec = self._validate_spec(spec, baseline_id)
        if invalid_spec is not None:
            return _error_result(
                "CONTRACT_MISMATCH",
                "The resolved baseline launch specification is invalid.",
                runtime_changed=False,
                details=invalid_spec,
            )
        authorization_error = self._authorization_result(
            "player.start",
            authorization_ref,
            baseline_id=baseline_id,
            session_id=spec.session_id,
        )
        if authorization_error is not None:
            return authorization_error

        existing = self._latest_active_for_baseline(state, baseline_id)
        if existing is not None:
            return self._reconcile_existing_start(state, existing, spec)

        try:
            located = self.process_adapter.locate(spec.session_id)
        except Exception:
            self._block_and_persist(state, baseline_id, "attached_process_lookup_failed")
            return _error_result(
                "STATE_UNKNOWN",
                "Existing player discovery failed; no duplicate start was attempted.",
                runtime_changed=None,
                recoverable=False,
            )
        if located.status == "unknown":
            self._block_and_persist(state, baseline_id, located.reason or "attached_process_lookup_unknown")
            return _error_result(
                "STATE_UNKNOWN",
                "Existing player state is unknown; no duplicate start was attempted.",
                runtime_changed=None,
                recoverable=False,
            )
        if located.status == "running":
            valid, reason = self._observation_matches_spec(located, spec)
            if not valid:
                self._block_and_persist(state, baseline_id, reason or "attached_identity_mismatch")
                return _error_result(
                    "STATE_UNKNOWN",
                    "An existing player was found but its session identity did not match the baseline.",
                    runtime_changed=None,
                    recoverable=False,
                )
            record = self._new_record(
                session_id=spec.session_id,
                launch_id=spec.launch_id,
                baseline_id=baseline_id,
                identity=located.identity,
                expected_executable_path=spec.executable_path,
                owned_by_provider=False,
                ownership_id=None,
                state="attached",
                stop_allowed=False,
            )
            state.sessions[record.session_id] = record
            self._write_state(state)
            return self._session_result(record, "attached_preserved", runtime_changed=False)
        if located.status != "not_found":
            self._block_and_persist(state, baseline_id, "invalid_process_lookup_result")
            return _error_result("STATE_UNKNOWN", "Existing player lookup returned no verifiable result.", runtime_changed=None, recoverable=False)

        intent = self._new_start_intent(spec)
        state.start_intents[baseline_id] = intent
        if not self._persist_unresolved(state, baseline_id):
            return _error_result(
                "STATE_UNKNOWN",
                "A durable start intent could not be recorded; no process start was attempted.",
                runtime_changed=None,
                recoverable=False,
            )
        environment = dict(spec.environment)
        environment[spec.session_file_env] = str(Path(spec.session_file_path))
        try:
            process = self.process_adapter.start(
                spec.executable_path,
                spec.arguments,
                spec.working_directory,
                environment,
            )
        except Exception:
            self._save_unknown_start(state, spec, "start_request_outcome_unknown", intent=intent)
            return _error_result(
                "STATE_UNKNOWN",
                "Player start outcome is unknown; automatic retry is blocked.",
                runtime_changed=None,
                recoverable=False,
            )
        if process is None:
            self._save_unknown_start(state, spec, "start_returned_no_process_handle", intent=intent)
            return _error_result(
                "STATE_UNKNOWN",
                "Player start returned no identity handle; automatic retry is blocked.",
                runtime_changed=None,
                recoverable=False,
            )
        prepare_session = getattr(self.process_adapter, "prepare_started_session", None)
        if callable(prepare_session):
            try:
                prepare_session(process, spec.session_id, spec.launch_id)
            except Exception:
                self._cleanup_started_process(process)
                self._save_unknown_start(state, spec, "started_process_session_binding_failed", intent=intent)
                return _error_result(
                    "STATE_UNKNOWN",
                    "The started player could not be bound to its configured session; automatic retry is blocked.",
                    runtime_changed=None,
                    recoverable=False,
                )
        try:
            observation = self.process_adapter.inspect(process)
        except Exception:
            observation = ProcessObservation.unknown("post_start_identity_read_failed")
        valid, reason = self._observation_matches_spec(observation, spec)
        if not valid:
            self._cleanup_started_process(process)
            self._save_unknown_start(state, spec, reason or "post_start_identity_unverified", observation.identity, intent=intent)
            return _error_result(
                "STATE_UNKNOWN",
                "Player started without a verifiable session identity; automatic retry is blocked.",
                runtime_changed=None,
                recoverable=False,
            )
        record = self._new_record(
            session_id=spec.session_id,
            launch_id=spec.launch_id,
            baseline_id=baseline_id,
            identity=observation.identity,
            expected_executable_path=spec.executable_path,
            owned_by_provider=True,
            ownership_id="owned_" + uuid4().hex,
            state="started",
            stop_allowed=True,
        )
        record_owned_process = getattr(self.process_adapter, "record_owned_process", None)
        if callable(record_owned_process):
            try:
                record_owned_process(observation)
            except Exception:
                cleanup_verified_absent = self._cleanup_started_process(process)
                if cleanup_verified_absent:
                    self._forget_owned_process(record)
                self._save_unknown_start(state, spec, "owned_process_registration_failed", observation.identity, intent=intent)
                return _error_result(
                    "STATE_UNKNOWN",
                    "The started player could not be durably registered; automatic retry is blocked.",
                    runtime_changed=None,
                    recoverable=False,
                )
        state.sessions[record.session_id] = record
        state.start_intents.pop(baseline_id, None)
        try:
            self._write_state(state)
        except Exception:
            cleanup_verified_absent = self._cleanup_started_process(process)
            if cleanup_verified_absent:
                self._forget_owned_process(record)
            self._save_unknown_start(state, spec, "player_state_persistence_failed", observation.identity, intent=intent)
            return _error_result(
                "STATE_UNKNOWN",
                "The started player could not be durably recorded; automatic retry is blocked.",
                runtime_changed=None,
                recoverable=False,
            )
        return self._session_result(record, "started", runtime_changed=True)

    def _reconcile_existing_start(
        self,
        state: PlayerState,
        record: PlayerSessionRecord,
        spec: PlayerLaunchSpec,
    ) -> dict[str, Any]:
        if record.state == "unknown":
            return _error_result(
                "STATE_UNKNOWN",
                "A prior player state is unresolved; reconcile it through player.attach before retrying start.",
                runtime_changed=None,
                recoverable=False,
            )
        if record.identity is None:
            self._block_baseline(state, spec.baseline_id, "active_record_has_no_identity")
            return _error_result("STATE_UNKNOWN", "The active player record has no process identity.", runtime_changed=None, recoverable=False)
        try:
            process = self.process_adapter.attach(record.identity.process_id)
        except Exception:
            process = None
        if process is None:
            self._mark_unknown(state, record, "active_process_could_not_be_attached")
            return _error_result("STATE_UNKNOWN", "The recorded player could not be re-attached; no duplicate start was attempted.", runtime_changed=None, recoverable=False)
        try:
            observation = self.process_adapter.inspect(process)
        except Exception:
            observation = ProcessObservation.unknown("active_process_identity_read_failed")
        valid, reason = self._observation_matches_record(observation, record)
        if not valid:
            self._mark_unknown(state, record, reason or "active_process_identity_mismatch")
            return _error_result("STATE_UNKNOWN", "The recorded player identity is not currently verifiable; no duplicate start was attempted.", runtime_changed=None, recoverable=False)
        label = "already_started" if record.owned_by_provider is True else "attached_preserved"
        return self._session_result(record, label, runtime_changed=False)

    def _attach(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id = arguments.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return _error_result("CONTRACT_MISMATCH", "sessionId is required.", runtime_changed=False)
        state = self.state_store.read()
        record = state.sessions.get(session_id)
        matching_intents = [intent for intent in state.start_intents.values() if intent.session_id == session_id]
        if len(matching_intents) > 1:
            return _error_result("STATE_UNKNOWN", "Multiple unresolved start intents target this session.", runtime_changed=None, recoverable=False)
        pending_intent = matching_intents[0] if matching_intents else None
        if record is not None and record.state == "stopped":
            return _error_result("CONTRACT_MISMATCH", "The session is already stopped.", runtime_changed=False)

        if record is not None and record.identity is not None:
            try:
                process = self.process_adapter.attach(record.identity.process_id)
            except Exception:
                process = None
            if process is None:
                self._mark_unknown(state, record, "recorded_process_could_not_be_attached")
                return _error_result("STATE_UNKNOWN", "The recorded player identity could not be re-attached.", runtime_changed=None, recoverable=False)
            try:
                observation = self.process_adapter.inspect(process)
            except Exception:
                observation = ProcessObservation.unknown("attached_identity_read_failed")
            valid, reason = self._observation_matches_record(observation, record)
            if not valid:
                self._mark_unknown(state, record, reason or "attached_identity_mismatch")
                return _error_result("STATE_UNKNOWN", "The recorded player identity did not match; no process action was taken.", runtime_changed=None, recoverable=False)
            if record.state == "unknown":
                record.state = "started" if record.owned_by_provider is True else "attached"
                record.stop_allowed = record.owned_by_provider is True
                record.updated_at_utc = _now_utc()
                if record.baseline_id is not None:
                    state.blocked_baselines.pop(record.baseline_id, None)
                    state.start_intents.pop(record.baseline_id, None)
                self._write_state(state)
            return self._session_result(record, "reattached", runtime_changed=False)

        prior_baseline_id = record.baseline_id if record is not None else (pending_intent.baseline_id if pending_intent is not None else None)
        prior_launch_id = record.launch_id if record is not None else (pending_intent.launch_id if pending_intent is not None else None)
        prior_executable_path = record.expected_executable_path if record is not None else (pending_intent.expected_executable_path if pending_intent is not None else None)
        try:
            located = self.process_adapter.locate(session_id)
        except Exception:
            return _error_result("STATE_UNKNOWN", "Player discovery failed; no process action was taken.", runtime_changed=None, recoverable=False)
        if located.status == "unknown":
            return _error_result("STATE_UNKNOWN", "Player discovery returned an unverifiable state.", runtime_changed=None, recoverable=False)
        if located.status == "not_found":
            if pending_intent is not None:
                return _error_result(
                    "STATE_UNKNOWN",
                    "The durable start intent remains unresolved; a definitive process reconciliation is required.",
                    runtime_changed=None,
                    recoverable=False,
                )
            return _error_result("CONTRACT_MISMATCH", "sessionId is not attached to a running player.", runtime_changed=False)
        if located.status != "running" or located.identity is None:
            return _error_result("STATE_UNKNOWN", "Player discovery returned no verifiable identity.", runtime_changed=None, recoverable=False)
        if located.session_id != session_id or not located.launch_id:
            return _error_result("STATE_UNKNOWN", "Discovered player session identity did not match the requested session.", runtime_changed=None, recoverable=False)
        if prior_launch_id is not None and located.launch_id != prior_launch_id:
            if record is not None:
                self._mark_unknown(state, record, "launch_id_mismatch")
            return _error_result("STATE_UNKNOWN", "Discovered player launch identity did not match the recorded session.", runtime_changed=None, recoverable=False)
        if prior_executable_path is not None and not _same_path(located.identity.executable_path, prior_executable_path):
            if record is not None:
                self._mark_unknown(state, record, "executable_path_mismatch")
            return _error_result("STATE_UNKNOWN", "Discovered player executable identity did not match the recorded session.", runtime_changed=None, recoverable=False)
        record = self._new_record(
            session_id=session_id,
            launch_id=located.launch_id,
            baseline_id=prior_baseline_id,
            identity=located.identity,
            expected_executable_path=prior_executable_path or located.identity.executable_path,
            owned_by_provider=False,
            ownership_id=None,
            state="attached",
            stop_allowed=False,
        )
        state.sessions[session_id] = record
        if prior_baseline_id is not None:
            state.blocked_baselines.pop(prior_baseline_id, None)
            state.start_intents.pop(prior_baseline_id, None)
        self._write_state(state)
        return self._session_result(record, "attached_preserved", runtime_changed=False)

    def _stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id = arguments.get("sessionId")
        authorization_ref = arguments.get("authorizationRef")
        authorization_error = self._authorization_result("player.stop", authorization_ref, session_id=session_id)
        if authorization_error is not None:
            return authorization_error
        if not isinstance(session_id, str) or not session_id:
            return _error_result("CONTRACT_MISMATCH", "sessionId is required.", runtime_changed=False)
        state = self.state_store.read()
        record = state.sessions.get(session_id)
        if record is None:
            return _error_result("CONTRACT_MISMATCH", "sessionId is not registered.", runtime_changed=False)
        if record.state == "stopped":
            return self._session_result(record, "already_stopped", runtime_changed=False)
        if record.state == "unknown":
            return _error_result("STATE_UNKNOWN", "The player identity is unresolved; stop is refused.", runtime_changed=None, recoverable=False)
        if record.owned_by_provider is not True or not record.stop_allowed:
            return _error_result(
                "CONFLICT",
                "Attached player processes are preserved; only a verified provider-owned process may be stopped.",
                runtime_changed=False,
            )
        if not isinstance(record.ownership_id, str) or not record.ownership_id.startswith("owned_"):
            self._mark_unknown(state, record, "owned_record_has_no_valid_ownership_marker")
            return _error_result("STATE_UNKNOWN", "The provider-owned player has no valid ownership marker; stop is refused.", runtime_changed=None, recoverable=False)
        if record.identity is None:
            self._mark_unknown(state, record, "owned_record_has_no_identity")
            return _error_result("STATE_UNKNOWN", "The provider-owned player has no verifiable identity; stop is refused.", runtime_changed=None, recoverable=False)
        try:
            process = self.process_adapter.attach(record.identity.process_id)
        except Exception:
            process = None
        if process is None:
            self._mark_unknown(state, record, "owned_process_could_not_be_attached")
            return _error_result("STATE_UNKNOWN", "The provider-owned process could not be re-attached; stop is refused.", runtime_changed=None, recoverable=False)
        try:
            before = self.process_adapter.inspect(process)
        except Exception:
            before = ProcessObservation.unknown("owned_process_identity_read_failed")
        valid, reason = self._observation_matches_record(before, record)
        if not valid:
            self._mark_unknown(state, record, reason or "owned_process_identity_mismatch")
            return _error_result("STATE_UNKNOWN", "PID, launch, or ownership identity did not match; stop was refused.", runtime_changed=None, recoverable=False)
        try:
            stopped = self.process_adapter.stop(process, self.stop_timeout_seconds)
        except Exception:
            self._release_process(process)
            self._mark_unknown(state, record, "stop_request_outcome_unknown")
            return _error_result("STATE_UNKNOWN", "Stop outcome is unknown; automatic retry is refused.", runtime_changed=None, recoverable=False)
        if stopped.status != "stopped":
            self._release_process(process)
            self._mark_unknown(state, record, stopped.reason or f"stop_{stopped.status}")
            return _error_result("STATE_UNKNOWN", "Stop did not produce a verified terminal process state; retry is refused.", runtime_changed=None, recoverable=False)
        try:
            after = self.process_adapter.inspect(process)
        except Exception:
            after = ProcessObservation.unknown("post_stop_identity_read_failed")
        # Keep a native handle open through this post-stop observation.  The
        # adapter releases it only after the provider has verified absence.
        self._release_process(process)
        if after.status != "not_found":
            self._mark_unknown(state, record, after.reason or "post_stop_state_not_terminal")
            return _error_result("STATE_UNKNOWN", "The process did not reach a verified absent state; retry is refused.", runtime_changed=None, recoverable=False)
        record.state = "stopped"
        record.stop_allowed = False
        record.updated_at_utc = _now_utc()
        state.blocked_baselines.pop(record.baseline_id or "", None)
        forget_owned_process = getattr(self.process_adapter, "forget_owned_process", None)
        if callable(forget_owned_process):
            try:
                forget_owned_process(record)
            except Exception:
                # The OS process is already verified absent.  Keep the terminal
                # provider state; a stale external registration is harmless and
                # will be revalidated against OS identity before reuse.
                pass
        self._write_state(state)
        return self._session_result(record, "stopped", runtime_changed=True)

    def _observation_matches_spec(
        self,
        observation: ProcessObservation,
        spec: PlayerLaunchSpec,
    ) -> tuple[bool, str | None]:
        if observation.status != "running":
            return False, observation.reason or "process_not_running"
        if observation.process is None or observation.identity is None:
            return False, "process_identity_incomplete"
        if observation.session_id != spec.session_id:
            return False, "session_id_mismatch"
        if observation.launch_id != spec.launch_id:
            return False, "launch_id_mismatch"
        if not _same_path(observation.identity.executable_path, spec.executable_path):
            return False, "executable_path_mismatch"
        if observation.identity.process_id <= 0 or not observation.identity.start_time_utc:
            return False, "process_identity_incomplete"
        return True, None

    def _observation_matches_record(
        self,
        observation: ProcessObservation,
        record: PlayerSessionRecord,
    ) -> tuple[bool, str | None]:
        if observation.status != "running":
            return False, observation.reason or "process_not_running"
        if observation.process is None or observation.identity is None or record.identity is None:
            return False, "process_identity_incomplete"
        if observation.session_id != record.session_id:
            return False, "session_id_mismatch"
        if observation.launch_id != record.launch_id:
            return False, "launch_id_mismatch"
        if not _same_path(observation.identity.executable_path, record.identity.executable_path):
            return False, "executable_path_mismatch"
        if (
            observation.identity.process_id != record.identity.process_id
            or observation.identity.start_time_utc != record.identity.start_time_utc
        ):
            return False, "pid_or_start_time_mismatch"
        return True, None

    @staticmethod
    def _latest_active_for_baseline(state: PlayerState, baseline_id: str) -> PlayerSessionRecord | None:
        candidates = [
            record
            for record in state.sessions.values()
            if record.baseline_id == baseline_id and record.state != "stopped"
        ]
        return max(candidates, key=lambda item: item.updated_at_utc, default=None)

    @staticmethod
    def _new_record(
        *,
        session_id: str,
        launch_id: str,
        baseline_id: str | None,
        identity: ProcessIdentity | None,
        expected_executable_path: str | None,
        owned_by_provider: bool | None,
        ownership_id: str | None,
        state: Literal["started", "attached", "unknown", "stopped"],
        stop_allowed: bool,
    ) -> PlayerSessionRecord:
        now = _now_utc()
        return PlayerSessionRecord(
            session_id=session_id,
            launch_id=launch_id,
            baseline_id=baseline_id,
            identity=identity,
            expected_executable_path=expected_executable_path,
            owned_by_provider=owned_by_provider,
            ownership_id=ownership_id,
            state=state,
            stop_allowed=stop_allowed,
            created_at_utc=now,
            updated_at_utc=now,
        )

    @staticmethod
    def _session_result(record: PlayerSessionRecord, state: str, *, runtime_changed: bool | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sessionId": record.session_id,
            "launchId": record.launch_id,
            "baselineId": record.baseline_id,
            "state": state,
            "identityVerified": record.identity is not None,
            "ownedByProvider": record.owned_by_provider,
            "stopAllowed": record.stop_allowed,
        }
        if record.identity is not None:
            result.update(record.identity.as_dict())
        return {
            "status": "completed",
            "result": {"session": result},
            "error": None,
            "runtimeChanged": runtime_changed,
            "facts": {},
            "artifacts": [],
            "timingsMs": {},
        }

    @staticmethod
    def _block_baseline(state: PlayerState, baseline_id: str, reason: str) -> None:
        state.blocked_baselines[baseline_id] = reason

    def _block_and_persist(self, state: PlayerState, baseline_id: str, reason: str) -> None:
        self._block_baseline(state, baseline_id, reason)
        self._persist_unresolved(state, baseline_id)

    def _persist_unresolved(self, state: PlayerState, baseline_id: str) -> bool:
        try:
            self._write_state(state)
            return True
        except Exception:
            marker = getattr(self.state_store, "write_unresolved_marker", None)
            if callable(marker):
                try:
                    marker(state)
                    return True
                except Exception:
                    pass
            # A broken store cannot honestly be reported as durable.  Keep an
            # in-flight block for this provider instance and return STATE_UNKNOWN
            # to prevent a second mutation in the same process.
            self._volatile_blocked_baselines.add(baseline_id)
            return False

    def _save_unknown_start(
        self,
        state: PlayerState,
        spec: PlayerLaunchSpec,
        reason: str,
        identity: ProcessIdentity | None = None,
        *,
        intent: PlayerStartIntent | None = None,
    ) -> None:
        record = self._new_record(
            session_id=spec.session_id,
            launch_id=spec.launch_id,
            baseline_id=spec.baseline_id,
            identity=identity,
            expected_executable_path=spec.executable_path,
            owned_by_provider=None,
            ownership_id=None,
            state="unknown",
            stop_allowed=False,
        )
        state.sessions[record.session_id] = record
        state.start_intents[spec.baseline_id] = intent or self._new_start_intent(spec)
        self._block_baseline(state, spec.baseline_id, reason)
        self._persist_unresolved(state, spec.baseline_id)

    @staticmethod
    def _new_start_intent(spec: PlayerLaunchSpec) -> PlayerStartIntent:
        now = _now_utc()
        return PlayerStartIntent(
            baseline_id=spec.baseline_id,
            session_id=spec.session_id,
            launch_id=spec.launch_id,
            expected_executable_path=spec.executable_path,
            created_at_utc=now,
            updated_at_utc=now,
        )

    def _mark_unknown(self, state: PlayerState, record: PlayerSessionRecord, reason: str) -> None:
        record.state = "unknown"
        record.owned_by_provider = None
        record.stop_allowed = False
        record.updated_at_utc = _now_utc()
        if record.baseline_id is not None:
            self._block_baseline(state, record.baseline_id, reason)
            self._persist_unresolved(state, record.baseline_id)

    def _cleanup_started_process(self, process: Any) -> bool:
        verified_absent = False
        try:
            stopped = self.process_adapter.stop(process, self.stop_timeout_seconds)
            if stopped.status == "stopped":
                try:
                    after = self.process_adapter.inspect(process)
                except Exception:
                    after = ProcessObservation.unknown("cleanup_post_stop_identity_read_failed")
                verified_absent = after.status == "not_found"
        except Exception:
            verified_absent = False
        finally:
            self._release_process(process)
        return verified_absent

    def _forget_owned_process(self, record: PlayerSessionRecord) -> None:
        forget = getattr(self.process_adapter, "forget_owned_process", None)
        if callable(forget):
            try:
                forget(record)
            except Exception:
                pass

    def _release_process(self, process: Any) -> None:
        release = getattr(self.process_adapter, "release", None)
        if callable(release):
            try:
                release(process)
            except Exception:
                pass

    def _write_state(self, state: PlayerState) -> None:
        self.state_store.write(state)


__all__ = [
    "AuthorizationDecision",
    "BaselineResolver",
    "CAPABILITY",
    "DenyAllAuthorization",
    "InMemoryPlayerStateStore",
    "JsonPlayerStateStore",
    "PlayerLaunchSpec",
    "PlayerProcessProvider",
    "PlayerSessionRecord",
    "PlayerStartIntent",
    "PlayerState",
    "ProcessAdapter",
    "ProcessIdentity",
    "ProcessObservation",
    "ProcessStopResult",
    "SESSION_FILE_ENV",
    "SubprocessProcessAdapter",
]
