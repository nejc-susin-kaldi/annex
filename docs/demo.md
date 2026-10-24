# Demo: Goat Arcade on Annex in 12 minutes

For Kaldi management. **The story:** this is the server of one client, Goat Arcade, which runs eight pinball and retro arcades. Its staff aren't developers. With an AI, they've built their own small internal tools, and those tools run behind the company login. Next to them is an off-the-shelf tool they host themselves. The data the tools read is curated by the client's database owner. Backups, alerts and oversight come with the platform. Kaldi and the client can see all of it.

The demo follows Goat Arcade's people through one morning:
- **maja**, a venue manager, opens her venue's dashboard and her tools;
- **boris**, the lead technician, checks his board;
- **petra**, in finance, opens her cash dashboard in Metabase;
- **sam** (the AI, really) builds a new tool live;
- **kaldi-admin** shows the safety net and the oversight;
- **tomaz**, the managing director, and maja look at the dashboards;
- the close is the data owner's angle.

Two of the client's early tools, one for venue figures and one for weekly takings, are not in the demo: Nejc decided they were dashboards, not apps (D115). The demo says so.

All data is synthetic. Who's who and what each person can open is in [goat-arcade/tour.md](goat-arcade/tour.md).

## Before the demo (about 20 minutes ahead)
1. **Reset**, from the repo. It removes every app that isn't a Goat Arcade example, along with its data (`tilt-watch` too, so the live build starts from nothing). It also reseeds Goat Arcade's database so the data ends yesterday, redeploys the five tools and runs the checks. Then it sets up the morning: boris's claims are released and one party request arrives. The script lists what it will do and asks before it starts.
   ```bash
   scripts/reset-demo.sh --usage
   ```
   - `--usage` adds 8 weeks of synthetic usage history, so the console's Usage pages have a shape. Leave it out for a console that shows only real use.
   - It takes 10–15 minutes. It ends with the apps, the passwords (`scripts/show-users.sh`) and the checks: both suites should say `failed 0`, and there should be no "Problems" list.
   - It doesn't change accounts, secrets, backups or Mailpit. The tools keep their own data: party requests, desk sign-ups, Kanboard cards.
2. **Tunnel.** The reset starts it. Leave the Mac awake and don't change networks. Check it with `scripts/tunnel.sh status`.
3. **Browsers.** One person per browser, because the login is one session per browser:
   - **Window A** (Chrome) is **maja**, who is in the users group. Open https://home.annex.home.arpa:8443 at the login page and don't sign in yet. In a second tab, sign in to Metabase (https://metabase.annex.home.arpa:8443) as `maja@goat-arcade.example`, with the same password as her Annex login. Open *Goat Arcade → Venues → Venue performance* once to warm it, and leave it open: it is the first thing she shows. Metabase is slow on the first load.
   - **Window B** (Chrome, incognito) is **kaldi-admin**. Sign in, then open three tabs:
     - the console, https://console.annex.home.arpa:8443;
     - Mailpit, https://mail.annex.home.arpa:8443;
     - https://league-desk.annex.home.arpa:8443, for the safety net.
   - **Window C** (Safari or Firefox) is **boris**. Sign in, open https://tech-board.annex.home.arpa:8443, and Kanboard (https://kanboard.annex.home.arpa:8443) in a second tab. In a third tab, sign in to Metabase as `tomaz@goat-arcade.example` and open *Goat Arcade → Management → Goat Arcade at a glance*.
   - **Window D** (a private window in Firefox or Safari, next to window C) is **petra**. Sign in to Metabase as `petra@goat-arcade.example` and open *Goat Arcade → Finance → Cash and collections*.
   - The passwords come from `scripts/show-users.sh`. All the hostnames are in `/etc/hosts` (`docs/setup.md` §3).
4. **Claude Code**, ready for the live build:
   - Create a fresh empty folder (the name doesn't matter):
     ```bash
     mkdir -p ~/annex-demo/run-$(date +%m%d-%H%M)
     ```
   - Start a new Code session in it.
   - Check that the `annex` MCP server is connected (`/mcp`).
   - Have the prompt below ready to paste.
5. **Terminal** in the repo, with a large font, for `scripts/demo-booking.sh`, `scripts/demo-bad-deploy.sh` and `./annex` commands.
6. **Fallback for the live build:** `examples/goat-arcade/tilt-watch` is a copy built from the same prompt. If the live build fails on stage, deploy it with `./annex deploy examples/goat-arcade/tilt-watch` (about a minute). To have it live from the start, run the reset with `--keep tilt-watch`. Its tile is then on the discovery page before the build, and the live build becomes a new release of it. If that release fails its health check, the copy stays live.

## The prompt for the live build
> Build tilt-watch on Annex, owner sam: for the last 30 days, each pinball machine's tilt rate next to its model's average. Flag anything over 3x, worst first. Deploy it and give me the URL.

What the data holds:
- `daily_plays` has games and tilts per machine per day, and `machine_floor` has each cabinet's model.
- The planted story is the **Cyber Samurai in Graz** (Goat Arcade Lend, machine #108). It tilts about 13% of its games; the other Cyber Samurai cabinets tilt about 2.5%, so it's about 5x.
- **The catch:** if the AI averages the model over all its cabinets, the Graz one is part of its own yardstick. It pulls the average up to about 5% and shows only about 2.6x, so nothing is flagged. If the page flags nothing, ask Claude Code: *"Compare each cabinet with the other cabinets of its model, not including itself."* The fix is quick, and it shows a review pass. The fallback's README tells the same story.

`tilt-watch` is in `/etc/hosts` and already in the Technical group on the discovery page (`config/teams.json`). A different name needs its own hosts entry (`docs/setup.md` §3) and lands under "Tools".

## Walkthrough
| Time | Do | Say |
|---|---|---|
| 0:00 | Title only, nothing on screen yet. | "Every company has small internal needs: a list for the technicians, a page for the tournaments, figures for the venue manager and the finance team. Today those wait for a dev team or end up in spreadsheets. Annex is a small platform on the client's own server, where the staff build these tools with AI and put them live safely. This is Goat Arcade's server: eight arcades, 145 machines, all synthetic." |
| 0:40 | **Start the live build:** paste the prompt into Claude Code and let it run. | "Sam, their operations manager, has a question about tilting machines. I'll give it to an AI now, and we'll come back to it." |
| 1:00 | **Window A**: sign in as `maja`. The discovery page, "Goat Arcade · internal tools", shows tiles grouped by team: Venues, Technical, Community, Platform. | "This is Maja, a venue manager. One login, Goat Arcade's own accounts. Every tool is listed with its owner and grouped by team, next to the platform services." |
| 1:30 | In **window A**'s second tab, open Metabase's **Venue performance**. Set the venue filter to Metelkova (her venue): *This week against last*, games per day, the weekday-by-hour heatmap, *Machine earnings*, *Idle machines*. Then switch the filter to Celje. | "Maja starts her morning with how her venue did this week against last, and which machines sit idle. She doesn't need a custom app for that: it's a dashboard on the same curated views, filtered to her venue. **Not everything needs an app: these two are dashboards.** We'll see the other one in a minute." |
| 2:30 | **Terminal**: `scripts/demo-booking.sh --child Luka`. It prints the form and "Annex answered: … HTTP 200". In window A, open **party-bookings** and reload: a new request sits on top, marked New. Open it to show the details, the history ("website") and the actions *Confirm*, *Decline* and *Mark as contacted*. In **window B**, Mailpit has "Party request: …" for maja. | "That was Goat Arcade's public website: a parent asking for a birthday party. It is the one route into Annex that comes from outside. Annex checks the website's secret, stores the request and retries it until the tool has it. Maja gets an email. The tool never emails the customer: Annex only sends mail to the company's own people." |
| 3:30 | **Window C**, as `boris`: **tech-board**. "Out of order now"; "Open tickets, oldest first"; *Rising faults*: "Haunted Lighthouse off since … 16 coil … faults", at the top. Click **I'm on it**; the row says "You since 09:12". Then the **Kanboard** tab: "Dashboard for Boris", the venue boards, and the Haunted Lighthouse card under *Goat Arcade Lent, Maribor (MB)*. | "Boris is the lead technician. Sam built this board for him. The machine at the top has been getting sicker for six weeks. 'I'm on it' is saved in the tool's own database, so the others see who took it. Next to it is Kanboard, an open-source tool they'd otherwise pay a SaaS for. It runs on the same server, behind the same login: no second password, with backups and monitoring like everything else." |
| 4:30 | **Window D**, as `petra`: Metabase's **Cash and collections**: weekly cash stacked by venue, the coins, tokens and card split, cash per machine by era, and counted cash against estimated plays per venue. | "Petra in finance wants last week's cash per venue, and the gap between the cash counted and what the plays say it should be. Early on someone built an app for this: a weekly job, a CSV, an email. A dashboard does it better, with nothing to maintain. Not everything needs an app: these two are dashboards. The apps are for things that write data, take calls from outside or run jobs." |
| 5:15 | **Claude Code**: show what it did. It read the contract and Goat Arcade's brief, started from a template, wrote the page and called `annex_deploy`. If a deploy failed and it fixed it, point that out. | "The AI follows a written contract: no login code, no passwords, no ports. When it gets something wrong, the platform says exactly what to fix. Its toolkit can't delete apps, set secrets or restore data: those stay with a person." (If asked: on this prototype that's a limit of the AI's toolkit, not yet a lock on the server.) |
| 6:15 | Open the URL it printed, in **window A**. Under "Flagged, worst first": **Cyber Samurai #108 (Goat Arcade Lend, Graz)**, about 13% against 2.5%, about 5x. Back on the discovery page, it's listed under Technical, owner `sam`. | "From an empty folder to a live tool, behind the same login, reading only the views it asked for. And it found the cabinet in Graz that tilts five times as often as its siblings. That's a tilt bob to adjust, or a cabinet that rocks." |
| 7:00 | **Terminal**: `scripts/demo-bad-deploy.sh`. It deploys a version of league-desk whose health check fails. The output ends with "failed the health gate … rolled back to rN, which is live again" and the releases. Reload league-desk in window B: unchanged. In the console: **league-desk** → *Releases* shows the failed release in red and the live one green. Click the failed one's **logs**: "the new version cannot reach its database", kept although that container is gone. | "Now the safety net. Someone, or their AI, ships a broken version. Annex checks it before switching, sees it's broken and puts the last good version back by itself. Lena's desk never noticed. The evidence stays, so whoever fixes it knows why." |
| 8:00 | **Window B**, the console's **Overview**: what needs attention first, then every tool with its owner, health, users this week, errors and last deploy. Open **Activity**: *Deploys* shows tilt-watch deployed a few minutes ago by sam, `via=mcp`. *Data reads* shows which views each tool read, per hour (`gateway.reads`; the newest hour appears once it's over). *Webhooks* has the booking; *Messages*, maja's party request. Then **window A**: maja opens https://console.annex.home.arpa:8443/status (it works), and `/apps/tech-board` gets a **403**: not her tool. Her own `/apps/party-bookings` opens. | "This is Kaldi's view, and the client's: every tool, who owns it, whether it's healthy, whether anyone uses it. One timeline shows who deployed what, which data each tool read and what was sent. Owners see their own tools; everyone else sees whether things are up. When something breaks, the owner gets an email before the users notice." |
| 9:15 | **Metabase.** **Window C**, as `tomaz`: *Goat Arcade at a glance* shows last week's cash and games against the week before, cash per venue over 26 weeks and the map. **Window A**, as `maja`: back on *Venue performance*, set the venue filter to Bežigrad: in the weekday × hour heatmap, Thursday evening stands out (league night). | "Analysts and managers get dashboards on the same curated views. Each team has its own collection. Everyone can explore with the query builder; only analysts write SQL. The dashboards are kept as code, so a reset rebuilds them." |
| 10:15 | Say it, or show `docs/goat-arcade/connect-your-database.md`. | "And the data owner's side. Goat Arcade's database stays theirs; in production it's a read replica. Annex gets one read-only login they can revoke at any time. Over it, a reviewed set of views without personal data: no names, emails, phones or birth dates leave their database. Every tool asks for views by name, and every read is logged with the tool's name. A new view is a reviewed change, not something an AI can grant itself." |
| 11:00 | Close with what's next. | "In production each client gets this on their own AWS account, with their company login (or Annex's own accounts if they don't have one) and the gateway pointed at a read replica. What you saw runs on one small VM." |
| 11:30 | Questions. | |

**If the live build is still running at 5:15,** do the safety net and the console (7:00–9:15) first and come back to it.

## If something goes wrong
| Symptom | Fix |
|---|---|
| A page doesn't load at all | The tunnel dropped: run `scripts/tunnel.sh up` |
| A new app's URL doesn't resolve | Its hostname isn't in `/etc/hosts`. With the wildcard resolver from setup.md §3 every name resolves; without it, only the names listed there do |
| Login keeps failing | Authelia blocks a user for 5 minutes after 5 wrong passwords. Use another user, or wait. `scripts/show-users.sh` prints the passwords |
| Metabase is slow or shows a spinner | It's still starting (up to about 2 minutes after a reset). Warm it before the demo |
| Claude Code asks who the owner is | Answer `sam` |
| tilt-watch deploys but flags nothing | The model average includes the cabinet itself (see "The prompt for the live build"). Ask: "Compare each cabinet with the other cabinets of its model, not including itself." |
| Claude Code's deploy fails repeatedly | Let it read the error once more. If it's stuck, show `./annex logs tilt-watch`, then deploy the fallback: `./annex deploy examples/goat-arcade/tilt-watch` |
| `demo-booking.sh` says BOOKING_HOOK_SECRET is not set | A human sets it once: `./annex secret set party-bookings BOOKING_HOOK_SECRET` (any long random value; the website and Annex share it) |
| tech-board shows someone "since" yesterday | A claim from a rehearsal. Click *Release*. The reset releases every claim the board shows |
| boris sees no boards in Kanboard | He hadn't opened Kanboard before the last seed. Run `./annex run kanboard seed`; it adds everyone who has signed in, and creates no card twice |
| The console says "no run seen yet" for tech-board's `watch` | Expected after a reset. The console and `annex status` count job runs from the scheduler's log, so a run by hand (`annex run`, which the reset uses) doesn't show there (D113) |
| Something looks broken on the platform | Run `scripts/test-smoke.sh` after the demo, not during it |

## After the demo
Nothing needs cleaning up. The live build stays until the next reset removes it. Run `scripts/reset-demo.sh` again before the next demo. When the demo stage is over, follow `docs/teardown.md`.
