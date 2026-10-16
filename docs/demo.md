# Demo: Annex in 10 minutes

For Kaldi management. **The story:** one client's own server, where people who aren't developers, with an AI, put small internal tools live behind the client's login in minutes. The data access is curated, and backups and oversight come built in.

The demo shows four things:
- the login and discovery page, from the user's side;
- two real-shaped tools (lookup, monthly report);
- **an AI building and deploying a new dashboard live**;
- the admin's oversight.

All data is synthetic.

## Before the demo (about 15 minutes ahead)
1. **Reset**, from the repo. Keep any app you want to show by adding `--keep <app>` (e.g. `--keep vibe-leads`). Everything else that isn't an example is removed with its data. The script lists what it will do and asks first.
   ```bash
   scripts/reset-demo.sh
   ```
   It takes 3–5 minutes and ends with the demo passwords and a check summary. Both suites should say `failed 0`.
2. **Tunnel.** The reset starts it. Leave the Mac awake and don't change networks. Check it with:
   ```bash
   scripts/tunnel.sh status
   ```
3. **Browser windows.**
   - **Window A** (normal), for the user's view: open https://home.annex.home.arpa:8443 at the Annex login page. Don't sign in yet.
   - **Window B** (private), for the admin's view:
     - sign in as `kaldi-admin`;
     - open https://logs.annex.home.arpa:8443 in one tab;
     - open https://metabase.annex.home.arpa:8443 in another and sign in to Metabase as `admin@annex.example` with the same password. Metabase is slow on the first load, so warm it now;
     - open https://mail.annex.home.arpa:8443 (Mailpit) in a third tab. It needs the `mail.annex.home.arpa` line in `/etc/hosts` (`docs/setup.md` §3).
4. **Claude Code**, ready for the live build:
   - Create a fresh empty folder; the folder name doesn't matter:
     ```bash
     mkdir -p ~/annex-demo/run-$(date +%m%d-%H%M)
     ```
   - Start a new Code session in it.
   - Check that the `annex` MCP server is connected (`/mcp`).
   - Have the prompt below ready to paste.
5. **Terminal** in the repo, with a large font, for `./annex` commands.
   Optional: `scripts/seed-usage.sh` adds 8 weeks of synthetic usage history so the "Annex usage" dashboard has lines (`--clear` removes it). Without it the dashboard shows only what the demo itself generated.
6. **Fallback:** if you have a dashboard already built by Claude Code (e.g. from the M0.4 proof run), keep it with `--keep <name>` in step 1.

## The prompt for the live build
> Build a dashboard on Annex called vibe-dashboard, owner sam. Show total assets under management, AUM by client segment and by advisor as charts, and the top 10 clients in a table. Greet the signed-in user by name. Deploy it and give me the URL.

`vibe-dashboard` is already in `/etc/hosts`. A different name needs its own hosts entry (`docs/setup.md` §3).

## Walkthrough
| Time | Do | Say |
|---|---|---|
| 0:00 | Title only; nothing on screen yet. | "Every client has small internal needs: a lookup, a monthly report, a dashboard. Today they wait for a dev team or end up in spreadsheets. Annex is a small platform on the client's own server where these tools can be built with AI and put live safely in minutes." |
| 0:45 | **Start the live build:** paste the prompt into Claude Code and let it run. | "I'll start an AI on a new tool now, and we'll come back to it in a few minutes." |
| 1:00 | **Window A**: sign in as `cm`. The discovery page appears. | "This is what a client's employee sees. One login, the client's own users. Every tool is listed with its owner, plus the platform services and optional add-ons." |
| 2:00 | Open **client-lookup**. Point at "Hello, Maja Horvat". Search `Novak`. | "The tool knows who I am, and it has no login code: the platform tells it. It reads client data through a curated gateway." |
| 2:45 | Scroll to **Data access for this app**. | "This tool asked for two views and got exactly those. The other two are denied by the gateway, not by the app. Every request is logged with the app's name." |
| 3:30 | Back to the discovery page, then open **monthly-report**. Show the summary, the top-clients table, *Download CSV* and the run history. | "This is the kind of SQL-to-CSV-to-email routine people run by hand every month. Here it runs on a schedule, keeps its history in its own database and its files on disk, and both are backed up every night." |
| 4:15 | **Terminal**: run `./annex run monthly-report monthly`, then reload the page; a new history row appears. Then show the **Mailpit** tab in window B: `cm` has an email, "Monthly positions: <month> is ready", with a download link. | "A builder can also run it on demand. Same command, same result. And when it finishes, the people who need the report get an email with the link. The tool never holds a mail password: it asks the platform to send, and the platform only sends to the client's own people. Here the mail is caught by a test inbox, so nothing leaves the server." |
| 5:00 | **Claude Code**: show what it's doing. It read the contract, chose the `static` template, wrote the page, and called `annex_deploy`. If a deploy failed and it fixed itself, point that out. | "The AI follows a written contract: no login code, no passwords, no ports. When it gets something wrong, the platform says exactly what to fix, and it fixes it. Its toolkit has no way to delete apps, set secrets or restore data: those stay with a person." (If asked: on this prototype that's a limit of the AI's toolkit, not yet a lock on the server; the production deploy path enforces it server-side.) |
| 6:30 | Open the URL it printed, in **window A**. "Hello, Maja Horvat" plus the charts. Back on the discovery page, it's listed with owner `sam`. | "Built and deployed from an empty folder, with no code from me. Behind the same login, and only the data it asked for." |
| 7:00 | **Terminal**: `scripts/demo-bad-deploy.sh`. It deploys a version of client-lookup whose health check fails; the output shows "failed the health gate … rolled back to rN, which is live again" and the app's releases. Reload client-lookup in window A: unchanged. | "Now the safety net. Someone, or their AI, ships a broken version. Annex checks it before switching, sees it's broken and puts the last good version back by itself, with the logs that say why. Users never saw it. Any release can also be rolled back by hand." |
| 7:30 | **Window B** (`kaldi-admin`): open **Logs** and pick `annex-app-vibe-dashboard` to see its requests. Then switch to window A and open `logs.annex.home.arpa:8443` as `cm`: **403**. Show the user counts on the tiles ("Users 7d") and the **Annex usage** dashboard in Metabase (Usage tile, or search "Annex usage"). Optionally also show the **Status** tile (every app and the platform, up or down; `status.annex.home.arpa` needs its hosts line) and, if one is handy, an alert email in Mailpit. | "Admins see everything that runs. Regular users don't. And the owner hears about a problem before their users do." |
| 8:15 | **Metabase**: New → Question → Product → *Client Summary* → Summarize: Sum of *Aum Eur* by *Segment* → Visualize. | "Analysts get Metabase on the same curated views, read-only. They can't see the raw tables." |
| 9:00 | Close with what's next. | "In production each client gets this on their own AWS account, with their company login (or Annex's own accounts if they don't have one) and the gateway pointed at a read replica. What you saw runs on one small VM." |
| 9:30 | Questions. | |

**If the live build is still running at 5:00,** do the oversight part (7:30–8:15) first and come back to it.

## If something goes wrong
| Symptom | Fix |
|---|---|
| A page doesn't load at all | The tunnel dropped: run `scripts/tunnel.sh up` |
| A new app's URL doesn't resolve | Its hostname isn't in `/etc/hosts`. With the wildcard resolver from setup.md §3 every name resolves; without it, only the names listed there do |
| Login keeps failing | Authelia blocks a user for 5 minutes after 5 wrong passwords. Use another user, or wait. `scripts/show-users.sh` prints the passwords |
| Metabase is slow or shows a spinner | It's still starting (up to about 2 minutes after a reset). Warm it before the demo |
| Claude Code asks who the owner is | Answer `sam` |
| Claude Code's deploy fails repeatedly | Let it read the error once more. If it's stuck, show `./annex logs vibe-dashboard`, and fall back to a pre-built dashboard kept with `--keep` |
| Something looks broken on the platform | Run `scripts/test-platform.sh` after the demo, not during it |

## After the demo
Nothing needs cleaning up. Run `scripts/reset-demo.sh` again before the next one. When the demo stage is over, follow `docs/teardown.md`.
