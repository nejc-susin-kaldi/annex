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

from annex_runtime import GatewayError, gateway, log


def main() -> int:
    t0 = time.monotonic()
    log.info("job started", job="daily")
    out_dir = Path(os.environ.get("DATA_DIR", "/tmp")) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rows = gateway("client_summary", order="aum_eur.desc", limit=20)
    except GatewayError as e:
        log.error("gateway read failed", view="client_summary", status=e.status, error=str(e))
        return 1
    path = out_dir / f"top-clients-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)
    log.info("job finished", job="daily", rows=len(rows), file=path.name, ms=round((time.monotonic() - t0) * 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
