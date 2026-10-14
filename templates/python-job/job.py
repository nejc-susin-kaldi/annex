"""__APP_NAME__: an Annex scheduled job.

Runs inside the app's container on the schedule in annex.yaml, or now with
`annex run __APP_NAME__ daily`. Exit code 0 means success; print what happened.
"""
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from annex_runtime import GatewayError, gateway


def main() -> int:
    out_dir = Path(os.environ.get("DATA_DIR", "/tmp")) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rows = gateway("client_summary", order="aum_eur.desc", limit=20)
    except GatewayError as e:
        print(f"gateway error: {e}", file=sys.stderr)
        return 1
    path = out_dir / f"top-clients-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
