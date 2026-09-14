from __future__ import annotations

from typing import Any

from host.errors import CommandError
from host.ledger import Ledger, dump_json, load_json, new_id, utc_now

FACT_COLUMNS = {
    "sourceSaved": "source_saved",
    "runtimeMatched": "runtime_matched",
    "checksPassed": "checks_passed",
    "visualReviewed": "visual_reviewed",
    "freshVerified": "fresh_verified",
}


class EvidenceStore:
    """Atomically records task facts with the provider evidence that supports each value."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_records (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    operation TEXT NOT NULL,
                    fact_name TEXT NOT NULL,
                    outcome INTEGER NOT NULL CHECK(outcome IN (0, 1)),
                    details_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_records(task_id, created_at)"
            )

    def apply_task_facts(
        self,
        task_id: str,
        operation: str,
        facts: dict[str, bool | None],
        evidence_by_fact: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        self.ledger.get_task(task_id)
        known = {name: value for name, value in facts.items() if value is not None}
        if not known:
            return self.ledger.get_task(task_id)
        if not known.keys() <= FACT_COLUMNS.keys():
            raise ValueError("Unknown task fact.")
        now = utc_now()
        assignments = []
        values: list[Any] = []
        with self.ledger.transaction() as connection:
            for fact_name, outcome in known.items():
                if type(outcome) is not bool:
                    raise ValueError("Known task facts must be booleans.")
                evidence = evidence_by_fact.get(fact_name, {})
                artifact_ids = evidence.get("artifactIds", [])
                details = evidence.get("details", {})
                connection.execute(
                    "INSERT INTO evidence_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("evidence"),
                        task_id,
                        operation,
                        fact_name,
                        int(outcome),
                        dump_json(details),
                        dump_json(artifact_ids),
                        now,
                    ),
                )
                assignments.append(f"{FACT_COLUMNS[fact_name]} = ?")
                values.append(int(outcome))
            values.append(task_id)
            changed = connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?",
                values,
            ).rowcount
            if changed != 1:
                raise CommandError("NOT_FOUND", "taskId is not registered.", stage="evidence")
        return self.ledger.get_task(task_id)

    def list_task(self, task_id: str) -> list[dict[str, Any]]:
        self.ledger.get_task(task_id)
        with self.ledger.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_records WHERE task_id = ? ORDER BY created_at, evidence_id",
                (task_id,),
            ).fetchall()
        return [
            {
                "evidenceId": row["evidence_id"],
                "taskId": row["task_id"],
                "operation": row["operation"],
                "fact": row["fact_name"],
                "outcome": bool(row["outcome"]),
                "details": load_json(row["details_json"], {}),
                "artifactIds": load_json(row["artifact_ids_json"], []),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def count(self) -> int:
        with self.ledger.transaction() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0])
