"""tech-board's scheduled jobs.

  python jobs.py morning   07:30 Mon-Sat: email the morning list (summary and a link) to TECH_RECIPIENTS
  python jobs.py watch     every 15 min: post to TECH_SLACK_CHANNEL when a machine newly goes out of order

Run one now with `annex run tech-board morning` (or `watch`). Both exit non-zero when they fail, so the owner hears.
"""
import os
import sys
import time

import board
import store
from annex_runtime import GatewayError, NotifyError, gateway, log, post_slack, send_email

CHANNEL = os.environ.get("TECH_SLACK_CHANNEL", "#ga-tech")


def recipients() -> list[str]:
    return [r.strip() for r in os.environ.get("TECH_RECIPIENTS", "boris").split(",") if r.strip()]


def morning() -> int:
    ml = board.load()
    lines = []
    if ml.out_of_order:
        lines.append("OUT OF ORDER")
        lines += [f"  {m['venue_code']:<4} #{m['machine_id']:<4} {m['title']}" for m in ml.out_of_order]
    if ml.tickets:
        lines.append("\nOPEN TICKETS, OLDEST FIRST")
        lines += [f"  {t['age_days']:>3} d  {t['venue_code']:<5} #{t['machine_id']:<4} {t['title']}: {t['symptom']}"
                  for t in ml.tickets[:12]]
        if len(ml.tickets) > 12:
            lines.append(f"  ... and {len(ml.tickets) - 12} more on the page")
    if ml.rising:
        lines.append("\nRISING FAULTS (coil + switch, last 7 days vs the 3 weeks before)")
        lines += [f"  {r.venue_code or 'store':<5} #{r.machine_id:<4} {r.title}: {r.recent} in its last week, "
                  f"{r.baseline_week:.1f}/week before"
                  + (f" (off the floor since {r.until:%-d %b})" if r.until != ml.data_end else "")
                  for r in ml.rising[:8]]
    day = ml.data_end.strftime("%a %-d %b")
    to = recipients()
    sent = send_email(to, f"Morning list: {len(ml.out_of_order)} out of order, {len(ml.tickets)} open tickets",
                      template="notice", data={
                          "title": f"Morning list (data to {day})",
                          "body": board.summary(ml) + "\n\nClaim what you're taking with \"I'm on it\" on the board.",
                          "link": os.environ["ANNEX_BASE_URL"] + "/", "link_text": "Open the board",
                          "details": "\n".join(lines)})
    log.info("morning list emailed", to=to, count=len(sent["to"]), out_of_order=len(ml.out_of_order),
             tickets=len(ml.tickets), rising=len(ml.rising), data_end=ml.data_end.isoformat())
    return 0


def watch() -> int:
    floor = gateway("machine_floor", select="machine_id,title,venue_code", out_of_order="is.true")
    current = {m["machine_id"]: {"title": m["title"], "venue_code": m["venue_code"]} for m in floor}
    with store.connect() as conn:
        previous = store.watch_previous(conn)
        if previous is None:
            store.watch_save(conn, current, 0, False)
            log.info("watch: first run, state recorded, nothing posted", out_of_order=len(current))
            return 0
        new = sorted(set(current) - set(previous))
        back = sorted(set(previous) - set(current))
        if not new:
            store.watch_save(conn, current, 0, False)
            log.info("watch: no machine newly out of order", out_of_order=len(current), back_in_service=len(back))
            return 0
        base = os.environ["ANNEX_BASE_URL"]
        items = [f"{current[m]['title']} (#{m}, {current[m]['venue_code']})" for m in new]
        text = (f":warning: Out of order: {'; '.join(items)}. {len(current)} machines out of order in total. "
                f"Board: {base}/")
        try:
            post_slack(CHANNEL, text)
        except NotifyError as e:
            # Keep the new ones out of the saved state, so the next run tries to post them again.
            store.watch_save(conn, {k: v for k, v in current.items() if k not in new}, len(new), False)
            if e.status == 503:  # Slack isn't connected (yet): handled, not a failure of the job
                log.warn("watch: Slack not connected, will retry", channel=CHANNEL, newly_out=new, error=str(e))
                return 0
            log.error("watch: Slack post failed", channel=CHANNEL, status=e.status, newly_out=new, error=str(e))
            return 1
        store.watch_save(conn, current, len(new), True)
        log.info("watch: posted newly out of order", channel=CHANNEL, newly_out=new, out_of_order=len(current))
        return 0


JOBS = {"morning": morning, "watch": watch}


def main() -> int:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job not in JOBS:
        log.error("unknown job", job=job, known=sorted(JOBS))
        return 2
    t0 = time.monotonic()
    log.info("job started", job=job)
    try:
        code = JOBS[job]()
    except GatewayError as e:
        log.error("gateway read failed", job=job, status=e.status, error=str(e))
        code = 1
    except NotifyError as e:
        log.error("email failed", job=job, status=e.status, error=str(e))
        code = 1
    except Exception:
        log.exception("job failed", job=job)
        code = 1
    log.info("job finished", job=job, exit=code, ms=round((time.monotonic() - t0) * 1000))
    return code


if __name__ == "__main__":
    sys.exit(main())
