# tech-board

Goat Arcade's technicians' morning list, on Annex. Owner `sam`, built for `boris` (lead technician).

- **The page** (`/`): machines out of order now, open service tickets oldest first (age, symptom, venue), and
  machines whose coil and switch faults are rising (their last 7 days against their weekly rate over the 21 days
  before; for a machine that's off the floor, the week before it went off). Dates are relative to the last day in
  the data, which the header shows.
- **"I'm on it"**: a button per machine and per ticket. The claim is stored in the app's own Postgres with the
  claimer's login and the time and shown to everyone ("Boris since 08:12"). Anyone can release a claim.
- **Jobs**: `morning` (07:30 Monday to Saturday) emails the list, with a short summary and a link to the page, to
  `TECH_RECIPIENTS` (default `boris`). `watch` (every 15 minutes) posts to `#ga-tech` when a machine newly goes
  out of order; its first run only records the state. If Slack isn't connected, it warns and tries again next run.

Views: `machine_floor`, `service_log`, `machine_events_daily`. Own data: `claims`, `watch_state`, `watch_runs`
(created on first use). Run a job by hand: `annex run tech-board morning` (or `watch`).
Unit tests for the pure helpers: `python3 -m unittest test_board` in this folder.

## How it was built

Sam's request, typed into Claude Code:

> Build tech-board on Annex, owner sam, for Boris and the technicians. One page for the morning: what's out of
> order right now, the open tickets oldest first with how old they are, the symptom and the venue, and machines
> whose coil and switch faults are going up (last week against the three weeks before; there's one machine that's
> been getting worse for weeks and it should be at the top). Give every machine and ticket an "I'm on it" button
> so the others can see who took it ("Boris since 08:12"), and let them release it. Email Boris the list at 07:30
> Monday to Saturday with a link to the page, and post to #ga-tech when a machine goes out of order.

**Review pass** (after the first deploy):
- The rising-faults list came up empty: the sick machine (Haunted Lighthouse, Maribor) went off the floor ten days
  before the end of the data, so it reported no faults in the data's last week. A machine that's off the floor now
  counts its last week up to the day it went off, and is marked "off since"; it tops the list (16 faults against
  5.7 a week before).
- Ticket ages and "off since" use the local (Ljubljana) date of the timestamp, not its UTC date.
- The database schema is created once per process instead of on every request; a page with no machine data shows
  a message instead of a 500.
- The email said "1 machines"; the subject and the body now read right.
