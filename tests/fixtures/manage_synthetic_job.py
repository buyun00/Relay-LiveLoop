from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "complete"))
    parser.add_argument("--tool-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()

    tool_root = Path(args.tool_root).resolve(strict=True)
    database = Path(args.database).resolve()
    sys.path.insert(0, str(tool_root))
    from host.ledger import Ledger

    ledger = Ledger(database)
    try:
        if args.mode == "seed":
            ledger.create_job(
                {
                    "jobId": args.job_id,
                    "requestId": "origin_synthetic_deployment_drain",
                    "operation": "iterate",
                    "state": "running",
                    "stage": "runtime_apply",
                    "runtimeChanged": False,
                }
            )
        else:
            ledger.update_job(
                args.job_id,
                state="completed",
                stage="complete",
                runtime_changed=False,
                result={"summary": "synthetic deployment drain completed"},
            )
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
