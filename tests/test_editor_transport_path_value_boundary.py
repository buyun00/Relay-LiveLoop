"""Synthetic malformed path-value coverage for the durable Editor transport."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


SUBJECT_ROOT = Path(os.environ["RELAY_LIVELOOP_SUBJECT"]).resolve()
if str(SUBJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUBJECT_ROOT))

from host.editor_transport import EditorJobEnvelope, EditorJobTicket, EditorJobTransport  # noqa: E402
from host.errors import CommandError  # noqa: E402


class EditorTransportPathValueBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-editor-path-boundary-")
        self.root = Path(self.temporary.name)
        self.job_root = self.root / "jobs"
        self.artifact_root = self.root / "artifacts"
        self.transport = EditorJobTransport(self.job_root, self.artifact_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def envelope(self, job_id: str) -> EditorJobEnvelope:
        requested = datetime.now(timezone.utc)
        return EditorJobEnvelope(
            job_id=job_id,
            kind="synthetic.inspect",
            input_snapshot="snapshot-synthetic",
            provider_id="provider-synthetic",
            artifact_root=str(self.artifact_root),
            requested_at_utc=requested.isoformat(),
            expires_at_utc=(requested + timedelta(minutes=2)).isoformat(),
            payload_json='{"target":"synthetic-target"}',
        )

    @staticmethod
    def result(ticket: EditorJobTicket, artifact_path: Any) -> dict[str, Any]:
        return {
            "jobId": ticket.job_id,
            "requestDigest": ticket.request_digest,
            "inputSnapshot": ticket.input_snapshot,
            "providerId": ticket.provider_id,
            "attemptId": "attempt-synthetic",
            "status": "completed",
            "completedAtUtc": datetime.now(timezone.utc).isoformat(),
            "resultJson": '{"outcome":"synthetic-only"}',
            "error": None,
            "artifacts": [
                {
                    "artifactId": "artifact-synthetic",
                    "kind": "synthetic-evidence",
                    "path": artifact_path,
                    "sha256": "0" * 64,
                    "mediaType": "application/octet-stream",
                    "size": 0,
                }
            ],
        }

    def publish_result(self, ticket: EditorJobTicket, value: dict[str, Any]) -> Path:
        destination = self.job_root / "results" / f"{ticket.job_id}.result.json"
        temporary = destination.with_name(f".{ticket.job_id}.synthetic.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
        return destination

    def assert_contract_failure(self, action: Callable[[], Any]) -> CommandError:
        with self.assertRaises(CommandError) as caught:
            action()
        error = caught.exception
        self.assertEqual("CONTRACT_MISMATCH", error.code)
        self.assertTrue(error.recoverable)
        self.assertFalse(error.runtime_changed)
        return error

    def test_json_non_path_types_are_controlled_without_rewrite_or_replay(self) -> None:
        malformed_values = [None, True, 7, [], {}]
        for index, malformed in enumerate(malformed_values):
            with self.subTest(path_type=type(malformed).__name__):
                envelope = self.envelope(f"job-json-path-{index}")
                ticket = self.transport.enqueue(envelope)
                request_path = self.job_root / "incoming" / f"{ticket.job_id}.request.json"
                request_bytes = request_path.read_bytes()
                request_mtime = request_path.stat().st_mtime_ns
                result_path = self.publish_result(ticket, self.result(ticket, malformed))
                result_bytes = result_path.read_bytes()

                first = self.assert_contract_failure(lambda: self.transport.poll_result(ticket))
                second = self.assert_contract_failure(lambda: self.transport.poll_result(ticket))
                self.assertEqual("validation", first.stage)
                self.assertEqual(first.code, second.code)
                self.assertEqual(result_bytes, result_path.read_bytes())

                retry = self.transport.enqueue(envelope)
                self.assertFalse(retry.submitted)
                self.assertEqual(ticket.request_digest, hashlib.sha256(request_bytes).hexdigest())
                self.assertEqual(request_bytes, request_path.read_bytes())
                self.assertEqual(request_mtime, request_path.stat().st_mtime_ns)

    def test_absolute_path_normalization_failure_is_controlled_and_durable(self) -> None:
        envelope = self.envelope("job-invalid-normalization")
        ticket = self.transport.enqueue(envelope)
        malformed = str(self.artifact_root / "synthetic-invalid") + "\0"
        result_path = self.publish_result(ticket, self.result(ticket, malformed))
        result_bytes = result_path.read_bytes()

        error = self.assert_contract_failure(lambda: self.transport.poll_result(ticket))
        self.assertEqual("validation", error.stage)
        self.assertEqual(result_bytes, result_path.read_bytes())
        self.assert_contract_failure(lambda: self.transport.poll_result(ticket))

    def test_malformed_request_artifact_root_is_rejected_before_publication(self) -> None:
        malformed_values = [None, True, 7, [], {}]
        for index, malformed in enumerate(malformed_values):
            with self.subTest(path_type=type(malformed).__name__):
                envelope = replace(
                    self.envelope(f"job-request-path-{index}"),
                    artifact_root=malformed,
                )
                error = self.assert_contract_failure(lambda: self.transport.enqueue(envelope))
                self.assertEqual("validation", error.stage)
                self.assertFalse(
                    (self.job_root / "incoming" / f"{envelope.job_id}.request.json").exists()
                )
                self.assertEqual([], list((self.job_root / "incoming").glob("*.tmp")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
