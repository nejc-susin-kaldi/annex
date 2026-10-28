# Goat Arcade on Annex: a self-guided tour

This server belongs to Goat Arcade d.o.o., a made-up client with eight pinball and retro arcades. Its staff built their own internal tools with an AI, and those tools run behind the company login. Kanboard runs next to them, self-hosted. Analysts have Metabase dashboards on curated data. Kaldi and the client oversee all of it from the console. **Everything here is synthetic**: the venues, machines, players and people.

This page lets you explore the server on your own, about 30–45 minutes for the whole tour. The 12-minute version with a presenter is [../demo.md](../demo.md).

## Before you start
- **Passwords.** Ask Nejc for the demo passwords (he prints them with `scripts/show-users.sh`). Each person below has one, and the same password signs them in to Metabase, with their email address as the login.
- **Addresses.** Everything is at `https://<name>.annex.home.arpa:8443`, through Nejc's SSH tunnel and `/etc/hosts` (`docs/setup.md` §3). Start at https://home.annex.home.arpa:8443.
- **One person per browser.** The login is one session per browser. To switch person, sign out at https://auth.annex.home.arpa:8443/logout, or use a second browser or a private window. Five wrong passwords lock a login for five minutes.
- **Data.** Goat Arcade's database holds 180 days of play. It ends the day before the last reset, so "this week" in the tools means the last 7 days in the data.

## Who's who
| Login | Person | Role | Annex groups | Open first |
|---|---|---|---|---|
| `maja` | Maja Horvat | venue manager, Metelkova (Ljubljana) | users | Metabase *Venue performance* (first), then **party-bookings** (she owns it); the console's Status page |
| `boris` | Boris Zajc | lead technician | users | **tech-board**, **Kanboard**; Metabase *Machine health* |
| `petra` | Petra Kralj | finance and admin | users | Metabase *Cash and collections* (no app: a dashboard does her job) |
| `lena` | Lena Gruber | community and tournaments, Graz | builders | **league-desk** and **high-score-wall** (she built both); Metabase *Players and tournaments* |
| `tomaz` | Tomaž Vidmar | managing director | users | Metabase *Goat Arcade at a glance* |
| `sam` | Sam Novak | operations manager, the one who builds tools | builders | owns **tech-board**, **Kanboard** (and **tilt-watch** when it's deployed); the only analyst in Metabase (SQL) |
| `kaldi-admin` | Kaldi Admin | Kaldi's platform admin | admins, builders | the **console** (everything), **Mailpit** (the test inbox), Dozzle (live logs), Metabase admin |

Metabase logins: `maja@goat-arcade.example` (likewise `boris`, `petra`, `lena`, `tomaz`), `sam@annex.example` and `admin@annex.example` for kaldi-admin.

## The tour
Take the stops in order. Each one names what it shows about Annex.

### 1. The front door (identity)
**As `maja`**, sign in at the home page. You get one login page, with Goat Arcade's logo, and then "Goat Arcade · internal tools". The tiles are grouped by team (Venues, Technical, Community, Platform), and each tool shows its owner.
- **What it shows:** the client's own accounts and one sign-in for every tool. None of the tools has login code: the platform tells each one who is signed in.

### 2. Venue performance (a dashboard, not an app)
**As `maja`**, open Metabase (stop 10) and *Venues → Venue performance*. Set the venue filter to Metelkova, her venue: this week against last, games per day, the weekday-by-hour heatmap, machine earnings and idle machines. Switch the filter to Celje.
- **What it shows:** **not everything needs an app.** A tool for a venue manager's read-only figures was built early on and then retired (D115): a Metabase dashboard on the same curated views does the job, with nothing to maintain. The apps below are the ones a dashboard can't be: they write data, take calls from outside or run jobs. The retired source is in git, at the tag `examples-archive` (`git show examples-archive:demo/apps/archive/venue-pulse/main.py`).

### 3. high-score-wall (a builder's second app, the gateway's limits)
**As anyone**, open high-score-wall and pick a venue: it's TV mode, one machine at a time with its all-time top 10. Press `F` for full screen.
- Then open `/api/data/daily_plays?limit=1` on the same host. The answer is **403**: this tool asked for three views, and `daily_plays` isn't one of them. The gateway refuses it, not the tool.
- **What it shows:** Lena built two tools (league-desk is the other), so ownership is per person. Data access is per tool, and every read is logged with the tool's name.

### 4. tech-board (its own database, jobs, email, Slack)
**As `boris`**, open tech-board. It shows what's out of order now, the open tickets oldest first and *Rising faults*. Click **I'm on it** on a ticket: the row now says who took it and since when, and everyone sees that. Release it again when you're done.
- **What it shows:** claims live in the tool's **own Postgres**, backed up every night. Two **scheduled jobs** run on the platform's scheduler:
  - `morning` (07:30) **emails** Boris the list;
  - `watch` (every 15 minutes) posts to **Slack** `#ga-tech` when a machine goes out of order.

### 5. Kanboard (a self-hosted, off-the-shelf tool)
**As `boris`**, open Kanboard. It shows "Dashboard for Boris" with no second password, a board per venue and "Parts to order", and a card per open repair. Each card links back to tech-board.
- **What it shows:** Annex isn't only for AI-built tools. Kanboard is an open-source tool that a company would otherwise rent as a SaaS. Here it runs under the same rules as the others: the login comes from the front door, it has its own database and backups, and the console watches it. How it was adapted is in `demo/apps/kanboard/README.md`.

### 6. Cash and collections (a dashboard, not an app)
**As `petra`**, open Metabase and *Finance → Cash and collections*: weekly cash by venue, the coins, tokens and card split, takings per machine by era, and counted cash against estimated plays per venue.
- **What it shows:** the second thing that doesn't need an app. A weekly-takings tool (a Monday job, a CSV, an email) was built and retired (D115): finance reads a dashboard instead. The retired source is in git, at the tag `examples-archive` (`git show examples-archive:examples/archive/weekly-takings/main.py`).

### 7. party-bookings (webhooks: the one way in from outside)
**As `maja`**, open party-bookings. These are birthday-party requests that came from Goat Arcade's public website. Open one: it shows the details, the customer's email as a link to reply by hand, the history (who did what) and the actions *Confirm*, *Decline* and *Mark as contacted*. Confirming one emails the venue manager a summary to forward to the parents.
- **What it shows:** **webhooks**. The website posts each request to `https://hooks.annex.home.arpa:8443/h/party-bookings/website`, the only address on the server that takes calls without a login. Annex checks the website's secret, stores every request and retries delivery until the tool has it. The tool never emails the customer: Annex sends mail only to the company's own people. (Nejc sends a new request with `scripts/demo-booking.sh`.)

### 8. league-desk (read-only data, the tool's own writes)
**As `lena`**, open league-desk. Up top is the Goat Arcade Masters: when, where and how many places are left. Sign a member up by nickname (try one from the entries list: it's refused, they're already entered), then withdraw the sign-up.
- **What it shows:** the gateway is **read-only**. Tournaments and players come from Goat Arcade's database; the sign-ups taken at the desk are the tool's own rows until the club system catches up. Each sign-up posts a line to `#ga-community`.

### 9. Mailpit and Slack (messages, safely)
**As `kaldi-admin`**, open https://mail.annex.home.arpa:8443. This is the test inbox that catches every email the server sends:
- Boris's "Morning list";
- Maja's "Party request: …" and "Confirmed: party for …";
- on Mondays, the weekly usage digest to the tool owners and admins.

Nothing leaves the server. **Slack** goes to a test workspace (ask Nejc to see it): `#ga-tech` and `#ga-community`. While a test suite runs, Annex is in **test mode**, and every post goes to `#annex-selftest` marked [TEST], so nobody is woken by a test.
- **What it shows:** tools send email and Slack only through the platform. They never hold a mail password or a Slack token. Every attempt is in the send log.

### 10. Metabase (dashboards and governance)
Sign in at https://metabase.annex.home.arpa:8443.
- **As `tomaz`:** *Goat Arcade → Management → Goat Arcade at a glance*.
- **As `maja`:** *Venues → Venue performance*. Change the venue filter and watch the weekday-by-hour heatmap.
- **Still as `maja`:** try *New → SQL query*. She can't: only analysts (`sam`) and admins write SQL. Everyone can explore the curated views with the query builder, and the views join on their keys.
- **What it shows:** analytics on the same curated, read-only views the tools use. There is a collection per team; each team edits its own and reads the others. The five dashboards are kept as code, so a reset rebuilds them.

### 11. The console (oversight)
https://console.annex.home.arpa:8443.
- **As `maja`:** *Status* shows whether things are up. Her own tool (party-bookings) opens with their summary, releases, usage and logs; `/apps/tech-board` is a 403, because it isn't hers.
- **As `kaldi-admin`:** *Overview* shows what needs attention first, then every tool with its owner, health, users this week, errors and last deploy, plus chips like "graduation candidate" or "dormant". *Platform* shows memory, disk, backups and the platform's own threads.
- **What it shows:** owners see their own tools; Kaldi and the client's admins see everything; everyone else sees whether things are up.

### 12. Activity (the audit trail)
**As `kaldi-admin`**, open the console's *Activity*. Use the chips:
- *Deploys*: who deployed what and how (`via=mcp` means through an AI);
- *Data reads*: which views each tool read, per hour;
- *Access*: sign-ins and account changes;
- *Messages*: emails and Slack posts;
- *Webhooks*: the website's requests;
- *Failures*.
- **What it shows:** one timeline answers "who did what, which data was read and what was sent".

### 13. Releases and rollback (the safety net)
**As `kaldi-admin`** (or as `lena`, for league-desk), open a tool's *Releases* in the console. A release that failed its health check is red, and its logs are still there, although its container is gone.
- If you have the `annex` CLI: `scripts/demo-bad-deploy.sh` ships a broken league-desk; Annex refuses to switch to it, and the working version stays live.
- `annex releases league-desk` lists the releases, and `annex rollback league-desk` switches back to an earlier one. Restoring a tool's *data* (`--with-data`) is human-only.
- **What it shows:** a broken deploy never reaches users, and the evidence stays.

## Hidden stories
The data was generated with a few stories in it. Here are hints, not answers. The answers are what `scripts/democlient.sh check` proves.
1. One machine in Maribor has been unwell for weeks. *tech-board's Rising faults; Machine health in Metabase.*
2. One player's nickname keeps turning up at the top of the high-score tables, at more than one venue. *high-score-wall; league-desk's player lookup.*
3. One venue's takings dipped about five weeks ago, after two popular tables moved away. Where did they go? *Metabase: Venue performance's machine earnings; Cash and collections' takings per machine by era.*
4. The arcades have a weekly rhythm, and one venue has a busy night of its own. *Venue performance's heatmap, venue by venue.*
5. Each era of pinball has its own character: one earns the most per game, another's games last the longest. *Goat Arcade at a glance, revenue by era.*
6. One cabinet tilts far more than its siblings. *Sam's live build in the demo looks for it: tilt-watch.*
7. Something is coming up soon, and people are signing up. *league-desk.*

## Where to read more
- What a builder and their AI get: [agent-brief.md](agent-brief.md), the views in [data-dictionary.md](data-dictionary.md).
- How the client's database is connected, and what never leaves it: [connect-your-database.md](connect-your-database.md).
- Each tool's README, with the prompt it was built from: `demo/apps/<tool>/README.md`.
- The platform on one page: [../architecture.md](../architecture.md).
