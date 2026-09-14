from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from host.editor_transport import EditorJobEnvelope, EditorJobTransport
from host.errors import CommandError


class EditorTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job_root = self.root / "editor-jobs"
        self.artifact_root = self.root / "artifacts"
        for name in ("incoming", "processing", "results"):
            (self.job_root / name).mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir()
        self.transport = EditorJobTransport(self.job_root, self.artifact_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def envelope(self, job_id: str = "job-synthetic") -> EditorJobEnvelope:
        now = datetime.now(timezone.utc)
        return EditorJobEnvelope(
            job_id=job_id,
            kind="source.locate",
            input_snapshot="snapshot-synthetic",
            provider_id="editor-synthetic",
            artifact_root=str(self.artifact_root),
            requested_at_utc=now.isoformat(),
            expires_at_utc=(now + timedelta(minutes=2)).isoformat(),
            payload_json=json.dumps({"sourceGuid": "guid-synthetic"}, separators=(",", ":")),
        )

    def result(self, ticket: object, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "jobId": ticket.job_id,
            "requestDigest": ticket.request_digest,
            "inputSnapshot": ticket.input_snapshot,
            "providerId": ticket.provider_id,
            "attemptId": "attempt-synthetic",
            "status": "completed",
            "completedAtUtc": datetime.now(timezone.utc).isoformat(),
            "resultJson": "{\"decision\":\"Located\"}",
            "error": None,
            "artifacts": [],
        }
        value.update(changes)
        return value

    def write_result(self, ticket: object, value: dict[str, object]) -> None:
        path = self.job_root / "results" / f"{ticket.job_id}.result.json"
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_enqueue_is_atomic_and_exact_retry_does_not_rewrite(self) -> None:
        envelope = self.envelope()
        first = self.transport.enqueue(envelope)
        path = self.job_root / "incoming" / f"{envelope.job_id}.request.json"
        before = path.read_bytes()
        second = self.transport.enqueue(envelope)
        self.assertTrue(first.submitted)
        self.assertFalse(second.submitted)
        self.assertEqual(first.request_digest, hashlib.sha256(before).hexdigest())
        self.assertEqual(first.request_digest, second.request_digest)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], list(path.parent.glob("*.tmp")))
        self.assertEqual(
            {
                "jobId", "kind", "inputSnapshot", "providerId", "artifactRoot",
                "requestedAtUtc", "expiresAtUtc", "payloadJson",
            },
            set(json.loads(before)),
        )

    def test_same_job_id_with_different_request_is_rejected(self) -> None:
        self.transport.enqueue(self.envelope())
        changed = self.envelope()
        changed = replace(changed, payload_json="{\"changed\":true}")
        with self.assertRaises(CommandError) as rejected:
            self.transport.enqueue(changed)
        self.assertEqual("INPUT_CHANGED", rejected.exception.code)

    def test_processing_request_is_treated_as_the_same_durable_submission(self) -> None:
        envelope = self.envelope()
        ticket = self.transport.enqueue(envelope)
        source = self.job_root / "incoming" / f"{envelope.job_id}.request.json"
        destination = self.job_root / "processing" / source.name
        source.replace(destination)
        replay = self.transport.enqueue(envelope)
        self.assertFalse(replay.submitted)
        self.assertEqual(ticket.request_digest, replay.request_digest)
        self.assertFalse(source.exists())

    def test_matching_completed_result_is_returned_and_bound_to_request(self) -> None:
        ticket = self.transport.enqueue(self.envelope())
        value = self.result(ticket, error={"code": "", "stage": "", "message": ""})
        self.write_result(ticket, value)
        result = self.transport.poll_result(ticket)
        self.assertEqual("completed", result["status"])
        self.assertIsNone(result["error"])
        self.assertEqual("Located", json.loads(result["resultJson"])["decision"])

    def test_result_digest_mismatch_is_never_accepted(self) -> None:
        ticket = self.transport.enqueue(self.envelope())
        self.write_result(ticket, self.result(ticket, requestDigest="0" * 64))
        with self.assertRaises(CommandError) as rejected:
            self.transport.poll_result(ticket)
        self.assertEqual("INPUT_CHANGED", rejected.exception.code)

    def test_failed_result_requires_structured_error(self) -> None:
        ticket = self.transport.enqueue(self.envelope())
        self.write_result(ticket, self.result(ticket, status="failed", resultJson=None, error=None))
        with self.assertRaises(CommandError) as rejected:
            self.transport.poll_result(ticket)
        self.assertEqual("CONTRACT_MISMATCH", rejected.exception.code)

    def test_artifact_root_escape_is_rejected_before_submission(self) -> None:
        envelope = self.envelope()
        escaped = replace(envelope, artifact_root=str(self.root))
        with self.assertRaises(CommandError) as rejected:
            self.transport.enqueue(escaped)
        self.assertEqual("AUTH_REQUIRED", rejected.exception.code)

    def test_wait_timeout_is_honest_and_recoverable(self) -> None:
        ticket = self.transport.enqueue(self.envelope())
        with self.assertRaises(CommandError) as timed_out:
            self.transport.wait(ticket, timeout_seconds=0.02, poll_interval_seconds=0.005)
        self.assertEqual("TIMEOUT", timed_out.exception.code)
        self.assertTrue(timed_out.exception.recoverable)


if __name__ == "__main__":
    unittest.main()
