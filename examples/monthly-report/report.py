"""Build the monthly positions report.

Reads monthly_positions through the Annex gateway, writes one CSV per month to
/data/reports and records the run in the app's database. Runs on the schedule
in annex.yaml, or now with: annex run monthly-report monthly
Optional argument: the month, e.g. `python report.py 2026-08`.
"""
import csv
import sys
import time
from collections import defaultdict

from annex_runtime import GatewayError, gateway
from store import REPORTS, connect

ASSET_CLASSES = ["Equity", "Bond", "Fund", "ETF", "Money market"]


def month_end_for(arg: str | None) -> str:
    if arg:
        rows = gateway("monthly_positions", select="month_end", month_end=f"gte.{arg}-01", order="month_end", limit=1)
    else:
        rows = gateway("monthly_positions", select="month_end", order="month_end.desc", limit=1)
    if not rows:
        raise SystemExit(f"no positions for {arg or 'any month'}")
    return rows[0]["month_end"]


def main() -> int:
    t0 = time.monotonic()
    try:
        month_end = month_end_for(sys.argv[1] if len(sys.argv) > 1 else None)
        rows = gateway("monthly_positions", month_end=f"eq.{month_end}",
                       select="client_id,client_name,asset_class,market_value_eur")
    except GatewayError as e:
        print(f"gateway error: {e}", file=sys.stderr)
        return 1

    per_client: dict[int, dict] = defaultdict(lambda: {c: 0.0 for c in ASSET_CLASSES})
    names = {}
    for r in rows:
        per_client[r["client_id"]][r["asset_class"]] += float(r["market_value_eur"])
        names[r["client_id"]] = r["client_name"]
    lines = sorted(({"client_id": cid, "client_name": names[cid], **vals, "total": sum(vals.values())}
                    for cid, vals in per_client.items()), key=lambda x: -x["total"])

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"monthly-positions-{month_end[:7]}.csv"
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["client_id", "client_name", *ASSET_CLASSES, "total_eur"])
        for x in lines:
            w.writerow([x["client_id"], x["client_name"], *(f"{x[c]:.2f}" for c in ASSET_CLASSES), f"{x['total']:.2f}"])
    tmp.replace(path)

    total = sum(x["total"] for x in lines)
    ms = int((time.monotonic() - t0) * 1000)
    with connect() as conn:
        conn.execute("INSERT INTO report_runs (month_end, clients, positions, total_value_eur, file, duration_ms) "
                     "VALUES (%s, %s, %s, %s, %s, %s)", (month_end, len(lines), len(rows), round(total, 2), path.name, ms))
    print(f"{path.name}: {len(lines)} clients, {len(rows)} positions, total {total:,.0f} EUR, {ms} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
