from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .errors import CommandError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_json(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


class Ledger:
    """Durable task, command, job, plan, artifact, and runtime-version ledger."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS commands (
            request_id TEXT PRIMARY KEY,
            command_hash TEXT NOT NULL,
            operation TEXT NOT NULL,
            task_id TEXT,
            state TEXT NOT NULL CHECK(state IN ('in_progress', 'completed')),
            response_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            target_json TEXT NOT NULL,
            reference_json TEXT,
            allowed_impact_json TEXT NOT NULL,
            acceptance_json TEXT NOT NULL,
            source_saved INTEGER,
            runtime_matched INTEGER,
            checks_passed INTEGER,
            visual_reviewed INTEGER,
            fresh_verified INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            session_id TEXT NOT NULL,
            input_snapshot TEXT NOT NULL,
            expected_runtime_revision TEXT,
            route TEXT NOT NULL,
            state TEXT NOT NULL,
            prepare_complete INTEGER NOT NULL CHECK(prepare_complete IN (0, 1)),
            approval_required INTEGER NOT NULL CHECK(approval_required IN (0, 1)),
            approval_ref TEXT,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            task_id TEXT REFERENCES tasks(task_id),
            plan_id TEXT REFERENCES plans(plan_id),
            state TEXT NOT NULL,
            stage TEXT NOT NULL,
            runtime_changed INTEGER,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
            result_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(task_id),
            job_id TEXT REFERENCES jobs(job_id),
            plan_id TEXT REFERENCES plans(plan_id),
            absolute_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            kind TEXT NOT NULL,
            media_type TEXT NOT NULL,
            original_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS versions (
            version_id TEXT PRIMARY KEY,
            session_id TEXT,
            scope TEXT NOT NULL,
            subject TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation >= 0),
            revision TEXT,
            artifact_id TEXT REFERENCES artifacts(artifact_id),
            applied_plan_id TEXT REFERENCES plans(plan_id),
            state TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, scope, subject, generation)
        );

        CREATE TABLE IF NOT EXISTS method_states (
            session_id TEXT NOT NULL,
            assembly_id TEXT NOT NULL,
            module_generation INTEGER NOT NULL CHECK(module_generation >= 0),
            method_id TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            applied_hash TEXT,
            artifact_id TEXT REFERENCES artifacts(artifact_id),
            applied_plan_id TEXT REFERENCES plans(plan_id),
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(session_id, assembly_id, module_generation, method_id)
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            plan_id TEXT NOT NULL REFERENCES plans(plan_id),
            user_confirmation_ref TEXT NOT NULL,
            approved_impact_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_plans_task ON plans(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_versions_subject ON versions(session_id, scope, subject, generation);
        """
        with self._lock:
            self._connection.executescript(schema)

    def begin_command(
        self, request_id: str, command_hash: str, operation: str, task_id: str | None
    ) -> tuple[str, dict[str, Any] | None]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT command_hash, state, response_json FROM commands WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                if row["command_hash"] != command_hash:
                    raise CommandError(
                        "CONTRACT_MISMATCH",
                        "requestId was already used for a different command.",
                        stage="idempotency",
                        runtime_changed=False,
                        recoverable=False,
                    )
                if row["state"] == "completed" and row["response_json"]:
                    return "replay", load_json(row["response_json"])
                raise CommandError(
                    "STATE_UNKNOWN",
                    "The prior attempt did not persist a terminal response; reconcile provider and runtime ledgers before any retry.",
                    stage="idempotency",
                    runtime_changed=None,
                    recoverable=False,
                )
            connection.execute(
                "INSERT INTO commands(request_id, command_hash, operation, task_id, state, created_at) VALUES (?, ?, ?, ?, 'in_progress', ?)",
                (request_id, command_hash, operation, task_id, utc_now()),
            )
        return "new", None

    def replay_command(self, request_id: str, command_hash: str) -> dict[str, Any] | None:
        """Return a durable prior result without registering a new command."""
        with self._lock:
            row = self._connection.execute(
                "SELECT command_hash, state, response_json FROM commands WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["command_hash"] != command_hash:
            raise CommandError(
                "CONTRACT_MISMATCH",
                "requestId was already used for a different command.",
                stage="idempotency",
                runtime_changed=False,
                recoverable=False,
            )
        if row["state"] == "completed" and row["response_json"]:
            return load_json(row["response_json"])
        raise CommandError(
            "STATE_UNKNOWN",
            "The prior attempt did not persist a terminal response; reconcile provider and runtime ledgers before any retry.",
            stage="idempotency",
            runtime_changed=None,
            recoverable=False,
        )

    def complete_command(self, request_id: str, response: dict[str, Any]) -> None:
        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE commands SET state = 'completed', response_json = ?, completed_at = ? WHERE request_id = ? AND state = 'in_progress'",
                (dump_json(response), utc_now(), request_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Command completion did not update exactly one in-progress row.")

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = new_id("task")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, session_id, goal, target_json, reference_json,
                    allowed_impact_json, acceptance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload["sessionId"],
                    payload["goal"],
                    dump_json(payload["target"]),
                    dump_json(payload.get("reference")),
                    dump_json(payload["allowedImpact"]),
                    dump_json(payload["acceptance"]),
                    now,
                    now,
                ),
            )
        return self.get_task(task_id)

    @staticmethod
    def _nullable_bool(value: Any) -> bool | None:
        return None if value is None else bool(value)

    def _task_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "taskId": row["task_id"],
            "sessionId": row["session_id"],
            "goal": row["goal"],
            "target": load_json(row["target_json"]),
            "reference": load_json(row["reference_json"]),
            "allowedImpact": load_json(row["allowed_impact_json"]),
            "acceptance": load_json(row["acceptance_json"]),
            "facts": {
                "sourceSaved": self._nullable_bool(row["source_saved"]),
                "runtimeMatched": self._nullable_bool(row["runtime_matched"]),
                "checksPassed": self._nullable_bool(row["checks_passed"]),
                "visualReviewed": self._nullable_bool(row["visual_reviewed"]),
                "freshVerified": self._nullable_bool(row["fresh_verified"]),
            },
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise CommandError("CONTRACT_MISMATCH", "taskId is not registered.", stage="lookup")
        return self._task_from_row(row)

    def update_task(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_task(task_id)
        columns = {
            "goal": "goal",
            "target": "target_json",
            "reference": "reference_json",
            "allowedImpact": "allowed_impact_json",
            "acceptance": "acceptance_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            assignments.append(f"{columns[key]} = ?")
            values.append(value if key == "goal" else dump_json(value))
        assignments.append("updated_at = ?")
        values.extend([utc_now(), task_id])
        with self.transaction() as connection:
            connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values)
        updated = self.get_task(task_id)
        if updated["sessionId"] != current["sessionId"]:
            raise RuntimeError("Task session identity changed unexpectedly.")
        return updated

    def update_task_facts(self, task_id: str, facts: dict[str, bool | None]) -> dict[str, Any]:
        mapping = {
            "sourceSaved": "source_saved",
            "runtimeMatched": "runtime_matched",
            "checksPassed": "checks_passed",
            "visualReviewed": "visual_reviewed",
            "freshVerified": "fresh_verified",
        }
        unknown = facts.keys() - mapping.keys()
        if unknown:
            raise ValueError(f"Unknown facts: {sorted(unknown)}")
        assignments = []
        values = []
        for key, value in facts.items():
            assignments.append(f"{mapping[key]} = ?")
            values.append(None if value is None else int(value))
        assignments.append("updated_at = ?")
        values.extend([utc_now(), task_id])
        with self.transaction() as connection:
            changed = connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values).rowcount
        if changed != 1:
            raise CommandError("CONTRACT_MISMATCH", "taskId is not registered.", stage="lookup")
        return self.get_task(task_id)

    def create_plan(self, record: dict[str, Any]) -> dict[str, Any]:
        task = self.get_task(record["taskId"])
        if task["sessionId"] != record["sessionId"]:
            raise CommandError("WRONG_SESSION", "Plan session does not match its task session.", stage="prepare")
        plan_id = record.get("planId") or new_id("plan")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO plans(
                    plan_id, task_id, session_id, input_snapshot, expected_runtime_revision,
                    route, state, prepare_complete, approval_required, approval_ref,
                    details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    record["taskId"],
                    record["sessionId"],
                    record["inputSnapshot"],
                    record.get("expectedRuntimeRevision"),
                    record["route"],
                    record["state"],
                    int(record["prepareComplete"]),
                    int(record["approvalRequired"]),
                    record.get("approvalRef"),
                    dump_json(record.get("details", {})),
                    now,
                    now,
                ),
            )
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise CommandError("CONTRACT_MISMATCH", "planId is not registered.", stage="lookup")
        return {
            "planId": row["plan_id"],
            "taskId": row["task_id"],
            "sessionId": row["session_id"],
            "inputSnapshot": row["input_snapshot"],
            "expectedRuntimeRevision": row["expected_runtime_revision"],
            "route": row["route"],
            "state": row["state"],
            "prepareComplete": bool(row["prepare_complete"]),
            "approvalRequired": bool(row["approval_required"]),
            "approvalRef": row["approval_ref"],
            "details": load_json(row["details_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def approve_plan(
        self, task_id: str, plan_id: str, user_confirmation_ref: str, approved_impact: dict[str, Any]
    ) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["taskId"] != task_id:
            raise CommandError("CONTRACT_MISMATCH", "planId does not belong to taskId.", stage="approval")
        required_impact = plan["details"].get("requiredImpact")
        if required_impact is not None and not self._impact_subset(required_impact, approved_impact):
            raise CommandError(
                "APPROVAL_REQUIRED",
                "approvedImpact does not cover the prepared plan's required impact.",
                stage="approval",
            )
        if not plan["prepareComplete"]:
            raise CommandError("INPUT_CHANGED", "The plan is not completely prepared.", stage="approval")
        approval_id = new_id("approval")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
                (approval_id, task_id, plan_id, user_confirmation_ref, dump_json(approved_impact), now),
            )
            connection.execute(
                "UPDATE plans SET approval_ref = ?, updated_at = ? WHERE plan_id = ?",
                (approval_id, now, plan_id),
            )
        return {
            "approvalId": approval_id,
            "taskId": task_id,
            "planId": plan_id,
            "userConfirmationRef": user_confirmation_ref,
            "approvedImpact": approved_impact,
            "createdAt": now,
        }

    @staticmethod
    def _impact_subset(candidate: dict[str, Any], boundary: dict[str, Any]) -> bool:
        for flag in ("hotfix", "restartPlayer", "buildBaseline"):
            if candidate[flag] and not boundary[flag]:
                return False
        for collection in ("rebuildViews", "reloadModules"):
            if not set(candidate[collection]).issubset(boundary[collection]):
                return False
        return True

    def create_job(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = record.get("jobId") or new_id("job")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, request_id, operation, task_id, plan_id, state, stage,
                    runtime_changed, result_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    record["requestId"],
                    record["operation"],
                    record.get("taskId"),
                    record.get("planId"),
                    record["state"],
                    record["stage"],
                    None if record.get("runtimeChanged") is None else int(record["runtimeChanged"]),
                    dump_json(record["result"]) if record.get("result") is not None else None,
                    dump_json(record["error"]) if record.get("error") is not None else None,
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise CommandError("CONTRACT_MISMATCH", "jobId is not registered.", stage="lookup")
        return {
            "jobId": row["job_id"],
            "requestId": row["request_id"],
            "operation": row["operation"],
            "taskId": row["task_id"],
            "planId": row["plan_id"],
            "state": row["state"],
            "stage": row["stage"],
            "runtimeChanged": self._nullable_bool(row["runtime_changed"]),
            "cancelRequested": bool(row["cancel_requested"]),
            "result": load_json(row["result_json"]),
            "error": load_json(row["error_json"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_active_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY created_at, job_id"
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def _job_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "jobId": row["job_id"],
            "requestId": row["request_id"],
            "operation": row["operation"],
            "taskId": row["task_id"],
            "planId": row["plan_id"],
            "state": row["state"],
            "stage": row["stage"],
            "runtimeChanged": self._nullable_bool(row["runtime_changed"]),
            "cancelRequested": bool(row["cancel_requested"]),
            "result": load_json(row["result_json"]),
            "error": load_json(row["error_json"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def update_job(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        runtime_changed: bool | None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET state = ?, stage = ?, runtime_changed = ?,
                    result_json = ?, error_json = ?, updated_at = ? WHERE job_id = ?
                """,
                (
                    state,
                    stage,
                    None if runtime_changed is None else int(runtime_changed),
                    dump_json(result) if result is not None else None,
                    dump_json(error) if error is not None else None,
                    utc_now(),
                    job_id,
                ),
            ).rowcount
        if changed != 1:
            raise CommandError("CONTRACT_MISMATCH", "jobId is not registered.", stage="lookup")
        return self.get_job(job_id)

    def request_job_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["state"] in {"completed", "failed", "state_unknown", "cancelled"}:
            return {"job": job, "cancelAccepted": False, "reason": "terminal"}
        if job["stage"] in {"runtime_apply", "runtime_reconcile"}:
            return {"job": job, "cancelAccepted": False, "reason": "non_interruptible_stage"}
        with self.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE job_id = ?",
                (utc_now(), job_id),
            )
        return {"job": self.get_job(job_id), "cancelAccepted": True, "reason": None}

    def register_artifact(self, record: dict[str, Any]) -> dict[str, Any]:
        artifact_id = record.get("artifactId") or new_id("artifact")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, task_id, job_id, plan_id, absolute_path, sha256,
                    size_bytes, kind, media_type, original_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    record.get("taskId"),
                    record.get("jobId"),
                    record.get("planId"),
                    record["absolutePath"],
                    record["sha256"],
                    record["sizeBytes"],
                    record["kind"],
                    record["mediaType"],
                    record["originalName"],
                    utc_now(),
                ),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise CommandError("CONTRACT_MISMATCH", "artifactId is not registered.", stage="artifact")
        return {
            "artifactId": row["artifact_id"],
            "taskId": row["task_id"],
            "jobId": row["job_id"],
            "planId": row["plan_id"],
            "absolutePath": row["absolute_path"],
            "sha256": row["sha256"],
            "sizeBytes": row["size_bytes"],
            "kind": row["kind"],
            "mediaType": row["media_type"],
            "originalName": row["original_name"],
            "createdAt": row["created_at"],
        }

    def record_version(self, record: dict[str, Any]) -> dict[str, Any]:
        version_id = record.get("versionId") or new_id("version")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO versions(
                    version_id, session_id, scope, subject, generation, revision,
                    artifact_id, applied_plan_id, state, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    record.get("sessionId"),
                    record["scope"],
                    record["subject"],
                    record["generation"],
                    record.get("revision"),
                    record.get("artifactId"),
                    record.get("appliedPlanId"),
                    record["state"],
                    dump_json(record.get("metadata", {})),
                    utc_now(),
                ),
            )
        return {"versionId": version_id, **record}

    def latest_version(self, session_id: str, scope: str, subject: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM versions
                WHERE session_id = ? AND scope = ? AND subject = ?
                ORDER BY generation DESC, created_at DESC
                LIMIT 1
                """,
                (session_id, scope, subject),
            ).fetchone()
        if row is None:
            return None
        return {
            "versionId": row["version_id"],
            "sessionId": row["session_id"],
            "scope": row["scope"],
            "subject": row["subject"],
            "generation": row["generation"],
            "revision": row["revision"],
            "artifactId": row["artifact_id"],
            "appliedPlanId": row["applied_plan_id"],
            "state": row["state"],
            "metadata": load_json(row["metadata_json"], {}),
            "createdAt": row["created_at"],
        }

    def set_method_state(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        values = (
            record["sessionId"],
            record["assemblyId"],
            record["moduleGeneration"],
            record["methodId"],
            record["sourceHash"],
            record.get("appliedHash"),
            record.get("artifactId"),
            record.get("appliedPlanId"),
            record["state"],
            now,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO method_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, assembly_id, module_generation, method_id)
                DO UPDATE SET source_hash=excluded.source_hash, applied_hash=excluded.applied_hash,
                    artifact_id=excluded.artifact_id, applied_plan_id=excluded.applied_plan_id,
                    state=excluded.state, updated_at=excluded.updated_at
                """,
                values,
            )
        return {**record, "updatedAt": now}

    def method_delta(
        self, session_id: str, assembly_id: str, module_generation: int, source_methods: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Compare source hashes to the current applied-method ledger, including reversions."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT method_id, source_hash, applied_hash, state FROM method_states
                WHERE session_id = ? AND assembly_id = ? AND module_generation = ?
                """,
                (session_id, assembly_id, module_generation),
            ).fetchall()
        applied = {row["method_id"]: row for row in rows}
        delta = []
        for method_id, source_hash in sorted(source_methods.items()):
            prior = applied.get(method_id)
            applied_hash = prior["applied_hash"] if prior else None
            if applied_hash != source_hash:
                delta.append(
                    {
                        "methodId": method_id,
                        "sourceHash": source_hash,
                        "appliedHash": applied_hash,
                        "change": "restore_or_update" if prior else "new_tracking_entry",
                    }
                )
        return delta

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = {}
            for table in ("tasks", "plans", "jobs", "artifacts", "versions"):
                counts[table] = self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            uncertain = self._connection.execute(
                "SELECT COUNT(*) FROM commands WHERE state = 'in_progress'"
            ).fetchone()[0]
        return {"counts": counts, "commandsRequiringReconciliation": uncertain}
