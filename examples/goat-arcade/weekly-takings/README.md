# weekly-takings

Goat Arcade's Monday takings report, for finance (Petra). Owner: `sam`. Replaces the old `monthly-report` example.

- **Job `weekly`** (Monday 07:00): takes the last complete Monday-to-Sunday week in the data (`revenue_weekly`; the
  latest day in `daily_plays` says which weeks are complete), writes `/data/takings/weekly-takings-<monday>.csv`
  (venue, amount, coins, tokens, card plays, machines, the week before and the change, plus a TOTAL row), records the
  run in its own Postgres, emails `TAKINGS_RECIPIENTS` (default `petra`) a short summary with a link to the run's page,
  and posts one line to `#ga-finance`. Running it again for the same week replaces that week's CSV and run (the run
  counter goes up, the email subject says "(updated)"); it never adds a second row.
  `python job.py 2026-10-05` redoes a past week; `--no-notify` skips email and Slack.
- **Page `/`:** the last complete week per venue with its change, the chain total, an 8-week sparkline per venue and
  for the chain, the run history with CSV links, and a note on running the job (builders see the command).
- **Page `/runs/<monday>`:** one run as recorded, read from its CSV, with the download button. The email links here.
- `/download/<file>.csv`: the CSV, behind login.

Run it now: `annex run weekly-takings weekly`. Tests for the pure helpers: `python3 -m unittest discover -s tests`.

## How it was built
Sam's request, typed into Claude Code:

> Build weekly-takings on Annex, owner sam, for Petra in finance. Every Monday at 7 it should take last week's cash
> per venue from revenue_weekly, write a CSV (amount, coins, tokens, card plays, machines and the change on the week
> before), keep a run history, email Petra a short summary with a link (no attachment), and post a line to
> #ga-finance. If I run it again for the same week it should replace that week, not add another. The page shows the
> latest week per venue with the change and the chain total, a small 8-week trend per venue, and the past runs with
> their CSVs. Start from the old monthly-report example.

Review pass after the first deploy:
- The "week before" total now counts every venue that had takings that week, also one that has since closed, so the
  chain change isn't flattered by a closure.
- Wording: the page says "data through <day>" and where the trend starts, instead of a clumsy "as far back as this
  page reads"; the "week before" tile shows EUR.
- Slack isn't connected on the local lane: the post fails with a warning and the job still succeeds (the email is the
  part that must work, and a failed email exits 1).
