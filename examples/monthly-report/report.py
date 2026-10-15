"""Build the monthly positions report.

Reads monthly_positions through the Annex gateway, writes one CSV per month to
/data/reports and records the run in the app's database. Runs on the schedule
in annex.yaml, or now with: annex run monthly-report monthly
Optional arguments: the month, e.g. `python report.py 2026-08`, and --notify, which emails
REPORT_RECIPIENTS and posts to REPORT_SLACK_CHANNEL when the report is done. The monthly job
uses --notify; the demo `refresh` job doesn't, so nobody's inbox fills up every 10 minutes.
"""
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date

from annex_runtime import GatewayError, NotifyError, gateway, post_slack, send_email
from store import REPORTS, connect

ASSET_CLASSES = ["Equity", "Bond", "Fund", "ETF", "Money market"]
TITLE = os.environ.get("REPORT_TITLE", "Monthly report")


def month_end_for(arg: str | None) -> str:
    if arg:
        rows = gateway("monthly_positions", select="month_end", month_end=f"gte.{arg}-01", order="month_end", limit=1)
    else:
        rows = gateway("monthly_positions", select="month_end", order="month_end.desc", limit=1)
    if not rows:
        raise SystemExit(f"no positions for {arg or 'any month'}")
    return rows[0]["month_end"]


def notify(title: str, month_end: str, clients: int, total: float, filename: str) -> int:
    """Email the recipients and post to Slack. A failed email fails the job (it is the point of the monthly
    run); a failed Slack post only prints a warning, because Slack is optional until a workspace is connected."""
    month = date.fromisoformat(month_end).strftime("%B %Y")
    link = f"{os.environ['ANNEX_BASE_URL']}/download/{filename}"
    recipients = [r.strip() for r in os.environ.get("REPORT_RECIPIENTS", "").split(",") if r.strip()]
    code = 0
    try:
        sent = send_email(recipients, f"{title}: {month} is ready", template="notice", data={
            "title": f"{title}: {month}",
            "body": f"Your report is ready: {clients} clients, total {total:,.0f} EUR at {month_end}.",
            "link": link, "link_text": "Download CSV"})
        print(f"emailed {', '.join(sent['to'])}")
    except NotifyError as e:
        print(f"email failed: {e}", file=sys.stderr)
        code = 1
    channel = os.environ.get("REPORT_SLACK_CHANNEL")
    if channel:
        try:
            post_slack(channel, f"{title}: {month} is ready: {link}")
            print(f"posted to {channel}")
        except NotifyError as e:
            print(f"warning: Slack post to {channel} failed: {e}", file=sys.stderr)
    return code


def main() -> int:
    t0 = time.monotonic()
    args = [a for a in sys.argv[1:] if a != "--notify"]
    try:
        month_end = month_end_for(args[0] if args else None)
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
    if "--notify" in sys.argv[1:]:
        return notify(TITLE, month_end, len(lines), total, path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
