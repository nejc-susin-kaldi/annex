"""The Monday job: last complete week's takings per venue as a CSV, a run record, an email and a Slack line.

Runs on the schedule in annex.yaml (Monday 07:00), or now with: annex run weekly-takings weekly
Options: a week's Monday (`python job.py 2026-10-05`) to redo a past week, and --no-notify to skip email and Slack.
Running it again for the same week replaces that week's CSV and run record; it doesn't add a second one.
Exits 1 when the data can't be read, the file or record can't be written, or the email fails (Slack is optional).
"""
import os
import sys
import time
from datetime import date

import store
from annex_runtime import GatewayError, NotifyError, log, post_slack, send_email
from takings import Week, csv_name, load, to_csv


def eur(x: float) -> str:
    return f"{x:,.0f} EUR"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def summary(week: Week) -> str:
    """The email body: the chain total and the biggest movers."""
    span = f"{week.week_start:%a %d %b} to {week.week_end:%a %d %b %Y}"
    lines = [f"Takings for the week {span}: {eur(week.total)} across {len(week.venues)} venues "
             f"({pct(week.change_pct)} on the week before)."]
    moved = sorted((v for v in week.venues if v.change_pct is not None), key=lambda v: v.change_pct)
    if len(moved) > 1:
        lines.append(f"Biggest rise: {moved[-1].venue_name} {pct(moved[-1].change_pct)}. "
                     f"Biggest drop: {moved[0].venue_name} {pct(moved[0].change_pct)}.")
    lines.append("The page has every venue, the 8-week trend and the CSV.")
    return "\n\n".join(lines)


def notify(week: Week, runs: int) -> int:
    """Email the recipients (a failure fails the job: the email is the point) and post one line to Slack (a failure
    is a warning: Slack may not be connected)."""
    link = f"{os.environ['ANNEX_BASE_URL']}/runs/{week.week_start.isoformat()}"
    recipients = [r.strip() for r in os.environ.get("TAKINGS_RECIPIENTS", "petra").split(",") if r.strip()]
    title = f"Weekly takings: week of {week.week_start:%d %b %Y}"
    code = 0
    try:
        sent = send_email(recipients, title + (" (updated)" if runs > 1 else ""), template="notice", data={
            "title": title, "body": summary(week), "link": link, "link_text": "Open the week"})
        store.mark_emailed(week.week_start, recipients)
        log.info("summary emailed", to=recipients, count=len(sent["to"]))  # logins, not addresses
    except NotifyError as e:
        log.error("email failed", status=e.status, error=str(e), week=week.week_start.isoformat())
        code = 1
    channel = os.environ.get("TAKINGS_SLACK_CHANNEL")
    if channel:
        try:
            post_slack(channel, f"Weekly takings, week of {week.week_start:%d %b}: {eur(week.total)} "
                                f"({pct(week.change_pct)} on the week before), {len(week.venues)} venues. {link}")
            log.info("posted to Slack", channel=channel)
        except NotifyError as e:
            log.warn("Slack post failed", channel=channel, status=e.status, error=str(e))
    return code


def main() -> int:
    t0 = time.monotonic()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    notify_on = "--no-notify" not in sys.argv[1:]
    log.info("job started", week=args[0] if args else "last complete", notify=notify_on)
    try:
        wanted = date.fromisoformat(args[0]) if args else None
        if wanted and wanted.weekday() != 0:
            raise ValueError(f"{args[0]} is not a Monday")
        week = load(wanted)
    except GatewayError as e:
        log.error("gateway read failed", status=e.status, error=str(e))
        return 1
    except (LookupError, ValueError) as e:
        log.error("no week to report", error=str(e))
        return 1

    store.FILES.mkdir(parents=True, exist_ok=True)
    path = store.FILES / csv_name(week.week_start)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(to_csv(week))
    tmp.replace(path)  # a re-run replaces the week's file in one step

    prev = week.prev_total
    runs = store.record({"week_start": week.week_start, "data_through": week.data_through, "venues": len(week.venues),
                         "total_eur": round(week.total, 2), "prev_total_eur": None if prev is None else round(prev, 2),
                         "file": path.name, "duration_ms": int((time.monotonic() - t0) * 1000)})
    if runs > 1:
        log.info("week run again: record and CSV replaced", week=week.week_start.isoformat(), runs=runs)
    code = notify(week, runs) if notify_on else 0
    log.info("job finished", week=week.week_start.isoformat(), data_through=week.data_through.isoformat(),
             venues=len(week.venues), total_eur=round(week.total), file=path.name, runs=runs,
             ms=int((time.monotonic() - t0) * 1000), ok=code == 0)
    return code


if __name__ == "__main__":
    sys.exit(main())
