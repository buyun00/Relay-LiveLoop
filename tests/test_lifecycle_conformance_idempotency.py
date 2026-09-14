"""Independent black-box Host lifecycle/idempotency conformance tests.

The process tests launch the real Relay LiveLoop Host as a subprocess on an
ephemeral loopback port with a temporary SQLite ledger and artifact root.  The
accepted-work replay test uses the real HTTP server and CommandService with a
neutral counting coordinator that only persists an accepted queued job.  It is
not evidence of a provider, Unity runtime, or Player success.

Set RELAY_LIVELOOP_SUBJECT to the exact source root under test.  After formal
integration the repository root is inferred when the variable is absent.
All identifiers, content, credentials, processes, and paths are synthetic and
test-owned.  No Player process is discovered, started, stopped, or accepted.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


TOKEN = "synthetic-lifecycle-bearer-token"
SUBJECT_ROOT = Path(
    os.environ.get("RELAY_LIVELOOP_SUBJECT", Path(__file__).resolve().parents[1])
).resolve()
if not (SUBJECT_ROOT / "relay_liveloop.py").is_file():
    raise RuntimeError(
        "Relay LiveLoop subject not found. Set RELAY_LIVELOOP_SUBJECT to the exact source root."
    )
if str(SUBJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUBJECT_ROOT))

from api.http_server import create_http_server  # noqa: E402
from host.artifacts import ArtifactStore  # noqa: E402
from host.ledger import Ledger  # noqa: E402
from host.service import CommandService  # noqa: E402


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    if port in {18760, 18761}:
        return ephemeral_port()
    return port


def allowed_impact() -> dict[str, Any]:
    return {
        "hotfix": False,
        "rebuildViews": [],
        "reloadModules": [],
        "restartPlayer": False,
        "buildBaseline": False,
    }


def task_arguments(goal: str = "Track one synthetic lifecycle task.") -> dict[str, Any]:
    return {
        "goal": goal,
        "sessionId": "synthetic-lifecycle-session",
        "target": "synthetic/lifecycle-target",
        "reference": "synthetic/lifecycle-reference",
        "allowedImpact": allowed_impact(),
        "acceptance": ["The synthetic lifecycle assertion is satisfied."],
    }


def command(
    operation: str,
    request_id: str,
    arguments: dict[str, Any],
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "protocolVersion": 1,
        "requestId": request_id,
        "operation": operation,
        "arguments": arguments,
    }
    if task_id is not None:
        value["taskId"] = task_id
    return value


def shutdown_request(request_id: str, wait: bool) -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "requestId": request_id,
        "mode": "graceful",
        "preservePlayer": True,
        "waitForActiveJobs": wait,
    }


def json_http(
    port: int,
    method: str,
    path: str,
    *,
    value: Any | None = None,
    token: str | None = TOKEN,
    timeout: float = 5,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if value is not None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        if "application/json" not in content_type:
            raise AssertionError(f"expected JSON response, got {content_type!r}: {raw!r}")
        return response.status, json.loads(raw.decode("utf-8"))
    finally:
        connection.close()


class HostProcessHarness(unittest.TestCase):
    """Own an isolated real Host subprocess and its disposable durable state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-liveloop-lifecycle-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "state" / "host.sqlite3"
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(parents=True)
        self.port = ephemeral_port()
        self.host_process: subprocess.Popen[str] | None = None
        self._start_host()

    def tearDown(self) -> None:
        self._finish_active_jobs()
        process = self.host_process
        self.host_process = None
        if process is not None and process.poll() is None:
            process.terminate()
        if process is not None:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.temporary.cleanup()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        paths = [str(SUBJECT_ROOT)]
        if environment.get("PYTHONPATH"):
            paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(paths)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["RELAY_LIVELOOP_TOKEN"] = TOKEN
        return environment

    def _start_host(self) -> None:
        self.host_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "relay_liveloop",
                "serve",
                "--database",
                str(self.database),
                "--artifact-root",
                str(self.artifact_root),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=self.root,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creation_flags(),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.host_process.poll() is not None:
                stdout, stderr = self.host_process.communicate()
                self.fail(f"Host exited during startup. stdout={stdout!r} stderr={stderr!r}")
            try:
                status, response = json_http(self.port, "GET", "/status")
                if status == 200 and response.get("service") == "Relay LiveLoop":
                    return
            except (ConnectionError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        self.fail("Host did not become ready on its synthetic ephemeral loopback port.")

    def _finish_active_jobs(self) -> None:
        if not self.database.exists():
            return
        ledger = Ledger(self.database)
        try:
            for job in ledger.list_active_jobs():
                ledger.update_job(
                    job["jobId"],
                    state="completed",
                    stage="synthetic_fixture_terminal",
                    runtime_changed=job["runtimeChanged"],
                    result={"summary": "Synthetic fixture cleanup only."},
                )
        finally:
            ledger.close()

    def _seed_job(self, *, job_id: str, stage: str, state: str = "queued") -> None:
        ledger = Ledger(self.database)
        try:
            ledger.create_job(
                {
                    "jobId": job_id,
                    "requestId": f"origin-{job_id}",
                    "operation": "iterate" if stage.startswith("runtime_") else "prepare",
                    "state": state,
                    "stage": stage,
                    "runtimeChanged": False,
                }
            )
        finally:
            ledger.close()

    def _task_count_and_goal(self, task_id: str) -> tuple[int, str]:
        ledger = Ledger(self.database)
        try:
            return int(ledger.summary()["counts"]["tasks"]), str(ledger.get_task(task_id)["goal"])
        finally:
            ledger.close()

    def _complete_job(self, job_id: str) -> None:
        ledger = Ledger(self.database)
        try:
            prior = ledger.get_job(job_id)
            ledger.update_job(
                job_id,
                state="completed",
                stage="synthetic_fixture_terminal",
                runtime_changed=prior["runtimeChanged"],
                result={"summary": "Synthetic job reached its arranged terminal state."},
            )
        finally:
            ledger.close()

    def _open_task(self, request_id: str, goal: str = "Track one synthetic lifecycle task.") -> tuple[dict[str, Any], dict[str, Any]]:
        request = command("task.open", request_id, task_arguments(goal))
        status, response = json_http(self.port, "POST", "/commands", value=request)
        self.assertEqual(200, status)
        self.assertEqual("completed", response["status"])
        return request, response

    def _drain_with_active_job(self, request_id: str) -> dict[str, Any]:
        status, response = json_http(
            self.port,
            "POST",
            "/lifecycle/shutdown",
            value=shutdown_request(request_id, True),
        )
        self.assertEqual(202, status)
        self.assertEqual("accepted", response["status"])
        self.assertEqual("draining", response["result"]["hostLifecycle"]["state"])
        self.assertFalse(response["result"]["hostLifecycle"]["safeToExit"])
        assert self.host_process is not None
        self.assertIsNone(self.host_process.poll(), "accepted drain must not mean process exit")
        return response

    def test_completed_mutation_exact_retry_replays_during_drain(self) -> None:
        original_request, original_response = self._open_task("request-completed-before-drain")
        task_id = original_response["result"]["taskId"]
        self._seed_job(job_id="job-holds-completed-replay-drain", stage="prepare_queued")
        self._drain_with_active_job("request-drain-completed-replay")

        replay_status, replay = json_http(
            self.port, "POST", "/commands", value=original_request
        )

        task_count, stored_goal = self._task_count_and_goal(task_id)
        self.assertEqual(1, task_count, "retry must not create a second task")
        self.assertEqual(original_request["arguments"]["goal"], stored_goal)
        self.assertEqual(200, replay_status, "exact durable retry must not become fresh work")
        self.assertEqual(original_response, replay)

    def test_same_id_altered_payload_stays_an_idempotency_collision_during_drain(self) -> None:
        original_request, original_response = self._open_task("request-altered-during-drain")
        task_id = original_response["result"]["taskId"]
        self._seed_job(job_id="job-holds-altered-replay-drain", stage="prepare_queued")
        self._drain_with_active_job("request-drain-altered-replay")

        altered_request = command(
            "task.open",
            original_request["requestId"],
            task_arguments("Altered synthetic goal must not replace the durable request."),
        )
        altered_status, altered = json_http(
            self.port, "POST", "/commands", value=altered_request
        )

        task_count, stored_goal = self._task_count_and_goal(task_id)
        self.assertEqual(1, task_count)
        self.assertEqual(original_request["arguments"]["goal"], stored_goal)
        self.assertEqual(400, altered_status)
        self.assertEqual("failed", altered["status"])
        self.assertEqual("CONTRACT_MISMATCH", altered["error"]["code"])
        self.assertEqual("idempotency", altered["error"]["stage"])

    def test_new_work_is_rejected_but_reads_remain_available_while_draining(self) -> None:
        _original_request, original_response = self._open_task("request-readable-task")
        task_id = original_response["result"]["taskId"]
        job_id = "job-readable-during-drain"
        self._seed_job(job_id=job_id, stage="prepare_queued")
        self._drain_with_active_job("request-drain-read-boundary")

        new_status, rejected = json_http(
            self.port,
            "POST",
            "/commands",
            value=command("task.open", "request-new-work-during-drain", task_arguments()),
        )
        self.assertEqual(400, new_status)
        self.assertEqual("CONFLICT", rejected["error"]["code"])
        self.assertEqual("host_lifecycle", rejected["error"]["stage"])

        status_code, status_response = json_http(
            self.port,
            "POST",
            "/commands",
            value=command("status", "request-status-during-drain", {}),
        )
        shown_code, shown = json_http(
            self.port,
            "POST",
            "/commands",
            value=command("task.show", "request-show-during-drain", {}, task_id=task_id),
        )
        job_code, job = json_http(
            self.port,
            "POST",
            "/commands",
            value=command("job.status", "request-job-during-drain", {"jobId": job_id}),
        )
        self.assertEqual((200, 200, 200), (status_code, shown_code, job_code))
        self.assertEqual("draining", status_response["result"]["hostLifecycle"]["state"])
        self.assertEqual(task_id, shown["result"]["task"]["taskId"])
        self.assertEqual(job_id, job["result"]["job"]["jobId"])
        task_count, _goal = self._task_count_and_goal(task_id)
        self.assertEqual(1, task_count)

    def test_unauthorized_shutdown_does_not_begin_drain(self) -> None:
        denied_status, denied = json_http(
            self.port,
            "POST",
            "/lifecycle/shutdown",
            value=shutdown_request("request-unauthorized-shutdown-state", False),
            token=None,
        )
        self.assertEqual(401, denied_status)
        self.assertEqual("AUTH_REQUIRED", denied["error"]["code"])

        status_code, status = json_http(self.port, "GET", "/status")
        self.assertEqual(200, status_code)
        lifecycle = status["hostLifecycle"]
        self.assertEqual("running", lifecycle["state"])
        self.assertTrue(lifecycle["acceptingCommands"])
        self.assertFalse(lifecycle["shutdownRequested"])
        self.assertIsNone(lifecycle["shutdownRequestId"])
        assert self.host_process is not None
        self.assertIsNone(self.host_process.poll())

    def test_shutdown_request_replay_and_collision_are_distinct(self) -> None:
        self._seed_job(job_id="job-holds-shutdown-replay", stage="prepare_queued")
        request = shutdown_request("request-shutdown-idempotency", True)
        first_status, first = json_http(
            self.port, "POST", "/lifecycle/shutdown", value=request
        )
        replay_status, replay = json_http(
            self.port, "POST", "/lifecycle/shutdown", value=request
        )
        altered = dict(request)
        altered["waitForActiveJobs"] = False
        altered_status, altered_response = json_http(
            self.port, "POST", "/lifecycle/shutdown", value=altered
        )

        self.assertEqual((202, 202), (first_status, replay_status))
        self.assertEqual(first, replay)
        self.assertEqual(400, altered_status)
        self.assertEqual("CONTRACT_MISMATCH", altered_response["error"]["code"])
        self.assertEqual("idempotency", altered_response["error"]["stage"])
        status_code, status = json_http(self.port, "GET", "/status")
        self.assertEqual(200, status_code)
        self.assertEqual("draining", status["hostLifecycle"]["state"])

    def test_noninterruptible_job_is_retained_until_terminal_and_sleeper_survives(self) -> None:
        job_id = "job-synthetic-runtime-apply"
        self._seed_job(job_id=job_id, stage="runtime_apply", state="running")
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
        )
        try:
            accepted = self._drain_with_active_job("request-drain-noninterruptible")
            lifecycle = accepted["result"]["hostLifecycle"]
            self.assertEqual([job_id], [job["jobId"] for job in lifecycle["nonInterruptibleJobs"]])
            self.assertFalse(lifecycle["nonInterruptibleJobs"][0]["interruptible"])
            self.assertIsNone(sleeper.poll(), "test-owned unrelated sleeper must still be alive")

            cli = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "relay_liveloop",
                    "command",
                    "job.cancel",
                    "--arguments",
                    json.dumps({"jobId": job_id}, separators=(",", ":")),
                    "--request-id",
                    "request-cancel-noninterruptible-during-drain",
                    "--url",
                    self.base_url,
                    "--json",
                ],
                cwd=self.root,
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=creation_flags(),
            )
            self.assertEqual(0, cli.returncode, cli.stderr)
            cancelled = json.loads(cli.stdout)
            self.assertFalse(cancelled["result"]["cancelAccepted"])
            self.assertEqual("non_interruptible_stage", cancelled["result"]["reason"])
            self.assertEqual("running", cancelled["result"]["job"]["state"])
            self.assertFalse(cancelled["result"]["job"]["cancelRequested"])
            assert self.host_process is not None
            self.assertIsNone(self.host_process.poll())

            self._complete_job(job_id)
            self.host_process.wait(timeout=5)
            self.assertEqual(0, self.host_process.returncode)
            self.assertIsNone(
                sleeper.poll(),
                "graceful Host exit must not terminate an unrelated synthetic process",
            )
        finally:
            if sleeper.poll() is None:
                sleeper.terminate()
            try:
                sleeper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sleeper.kill()
                sleeper.wait(timeout=5)


class CountingCoordinator:
    """Neutral fixture: persist one accepted queue row, never run a provider."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.prepare_calls = 0

    def enqueue_prepare(self, prepared_command: dict[str, Any]) -> dict[str, Any]:
        self.prepare_calls += 1
        return self.ledger.create_job(
            {
                "jobId": "job-synthetic-accepted-prepare",
                "requestId": prepared_command["requestId"],
                "operation": "prepare",
                "taskId": prepared_command["taskId"],
                "state": "queued",
                "stage": "prepare_queued",
                "runtimeChanged": False,
            }
        )

    def capability_states(self) -> dict[str, dict[str, Any]]:
        return {
            "preparation": {
                "capability": "preparation",
                "available": True,
                "providerId": "synthetic-counting-coordinator",
                "reason": "Neutral lifecycle test fixture; no provider execution.",
                "verified": False,
            }
        }

    def close(self) -> None:
        return None


class AcceptedReplayHTTPConformance(unittest.TestCase):
    """Exercise accepted-result replay through the real loopback HTTP adapter."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-liveloop-accepted-replay-")
        root = Path(self.temporary.name)
        artifact_root = root / "artifacts"
        artifact_root.mkdir(parents=True)
        self.ledger = Ledger(root / "state" / "host.sqlite3")
        self.coordinator = CountingCoordinator(self.ledger)
        self.service = CommandService(
            self.ledger,
            ArtifactStore(self.ledger, [artifact_root]),
            coordinator=self.coordinator,
        )
        self.server = create_http_server(self.service, TOKEN, "127.0.0.1", 0)
        self.port = int(self.server.server_address[1])
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="synthetic-lifecycle-http-server",
            daemon=True,
        )
        self.server_thread.start()

    def tearDown(self) -> None:
        for job in self.ledger.list_active_jobs():
            self.ledger.update_job(
                job["jobId"],
                state="completed",
                stage="synthetic_fixture_terminal",
                runtime_changed=job["runtimeChanged"],
                result={"summary": "Synthetic fixture cleanup only."},
            )
        self.server_thread.join(timeout=2)
        if self.server_thread.is_alive():
            self.server.shutdown()
            self.server_thread.join(timeout=2)
        self.server.server_close()
        self.service.close()
        self.ledger.close()
        self.temporary.cleanup()

    def test_accepted_mutation_exact_retry_replays_without_second_enqueue_during_drain(self) -> None:
        open_status, opened = json_http(
            self.port,
            "POST",
            "/commands",
            value=command(
                "task.open",
                "request-open-for-accepted-replay",
                task_arguments("Queue one neutral synthetic prepare operation."),
            ),
        )
        self.assertEqual(200, open_status)
        task_id = opened["result"]["taskId"]
        prepare_request = command(
            "prepare",
            "request-accepted-before-drain",
            {},
            task_id=task_id,
        )
        accepted_status, accepted = json_http(
            self.port, "POST", "/commands", value=prepare_request
        )
        self.assertEqual(200, accepted_status)
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual(1, self.coordinator.prepare_calls)

        drain_status, drain = json_http(
            self.port,
            "POST",
            "/lifecycle/shutdown",
            value=shutdown_request("request-drain-accepted-replay", True),
        )
        self.assertEqual(202, drain_status)
        self.assertEqual("draining", drain["result"]["hostLifecycle"]["state"])
        self.assertTrue(self.server_thread.is_alive())

        replay_status, replay = json_http(
            self.port, "POST", "/commands", value=prepare_request
        )

        self.assertEqual(1, self.coordinator.prepare_calls, "retry must not enqueue twice")
        self.assertEqual(1, self.ledger.summary()["counts"]["jobs"])
        self.assertEqual(200, replay_status, "exact accepted retry must return its durable result")
        self.assertEqual(accepted, replay)


if __name__ == "__main__":
    unittest.main()
