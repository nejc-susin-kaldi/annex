"""__APP_NAME__: an Annex scheduled job.

Runs inside the app's container on the schedule in annex.yaml, or now with
`annex run __APP_NAME__ daily`. Exit code 0 means success and non-zero failure (that is what alerts the
owner). Log with `log`: a start line and an end line with counts and duration, errors with context.
"""
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from annex_runtime import log


def main() -> int:
    t0 = time.monotonic()
    log.info("job started", job="daily")
    out_dir = Path(os.environ.get("DATA_DIR", "/tmp")) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    # The job's work goes here. To read a data view, list it under data.views in annex.yaml (`annex contract` names
    # this instance's views) and call gateway("<view>", order="day.desc", limit=20) from annex_runtime; on a
    # GatewayError, log.error it with the view and return 1.
    rows = [{"ran_at": datetime.now().isoformat(timespec="seconds")}]
    path = out_dir / f"run-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    log.info("job finished", job="daily", rows=len(rows), file=path.name, ms=round((time.monotonic() - t0) * 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
