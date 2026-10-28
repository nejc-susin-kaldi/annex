# kanboard

[Kanboard](https://kanboard.org) (MIT licence), an off-the-shelf open-source kanban board, self-hosted on Annex as
an ordinary Annex app. Owner `sam`; used by `boris` (lead technician) and the venue managers. Team: Technical
(`platform/config/teams.json`).

## What Goat Arcade uses it for

The technicians' **repair board**:

- **A project per open venue** ("Goat Arcade Metelkova, Ljubljana (LJ1)", ...). Each has a card per broken machine
  and the columns Reported, Diagnosing, Waiting for parts, Fixed.
- **"Parts to order"**, the same columns, with a card per repair that waits for parts.
- **"Workshop (machines in storage)"**, made only if a machine that isn't on a floor has an open ticket.

`tech-board` is the morning list, read straight from the data. Kanboard is where the technicians plan and talk about
the repair: they move cards, comment, attach photos of the broken coil, and set due dates. The two link up: every
seeded card links back to tech-board.

It shows that Annex isn't only for AI-built tools. You can bring the tools you'd otherwise pay a SaaS for, and they
get the same login, backups, health checks, alerts, logs and console as everything else.

## Why it fits Annex

- **One sign-in.** Kanboard supports reverse-proxy authentication: it trusts a header with the login. Annex's front
  door sets `Remote-User`, `Remote-Email` and `Remote-Name` from the session. It also **strips any client-sent copy
  of those headers**, so on a request that reaches the app they are trustworthy. Kanboard creates the user on their
  first visit, with name and email. `kaldi-admin` is the Kanboard admin (`KANBOARD_DEFAULT_ADMIN`); everyone else is
  a normal user. There is no second password.
- **Its own Postgres** (`storage.postgres`). Kanboard reads `DATABASE_URL` itself.
- **Files in `/data`** (`storage.files`): attachments, plugins and cache.
- **Backups for free.** Annex backs up the app's database and `/data` every night, and `annex rollback
  --with-data` (human only) restores both.
- **The console and alerts.** Health is Kanboard's own `/healthcheck.php` (200 when the database answers). Usage is
  counted at the front door. Logs are searchable with `annex logs kanboard`, and the two jobs show up in
  `annex status kanboard`.

## What was adapted to the contract, and why

The image is the **official release image, `kanboard/kanboard:v1.2.54`**, pinned by version and multi-arch digest.
It is Alpine with PHP 8.4, php-fpm and nginx 1.30, with amd64 and arm64 builds; 1.2.54 was released on 29 Aug 2026.
1.2.55 and 1.2.56 came out on 21 Oct 2026, less than the two weeks D3 asks for.

A thin wrapper adapts it (`Dockerfile`, `annex/`).

| Contract rule | Upstream image | The wrapper |
|---|---|---|
| no root, runs as uid 10001, ends with a non-root `USER` | entrypoint runs as root (`chown`, `s6-svscan`), php-fpm drops to `nginx` | `USER 10001`; `annex/entrypoint.sh` starts php-fpm and nginx itself (no s6, which writes into `/etc`) and stops the container when either exits |
| read-only root; only `/tmp` and `/data` writable | writes `/var/run`, `/var/lib/nginx`, `/etc/nginx/ssl`, `/var/www/app/data`, `/var/www/app/plugins` | nginx's pid, temp dirs and rendered config, php-fpm's pid and socket, PHP uploads all go to `/tmp`; Kanboard's data, files, cache and plugins go to `/data` (`annex/config.php`) |
| no anonymous volumes outside `/data` | `VOLUME` lines for data, plugins and SSL | the upstream filesystem is copied into a `FROM scratch` image, which keeps every file but drops the `VOLUME`, `ENTRYPOINT` and `HEALTHCHECK` metadata |
| listens on `$PORT` (above 1024), plain HTTP | ports 80 and 443 with a self-signed certificate | nginx listens on `$PORT` only (8080); TLS ends at the front door |
| no capabilities, `no-new-privileges` | needs root to start | nothing needs a capability |
| health check path | `/healthcheck.php` on port 80 | the same page on `$PORT`; the Docker `HEALTHCHECK` uses `curl`, which the image has |
| memory within 256 MB | php-fpm up to 20 workers | at most 5 workers, PHP capped at 96 MB per request; measured: **29 MiB** idle and after the checks |
| no platform labels (D83) | only OCI labels | none: `FROM scratch` drops them, and the Dockerfile adds no `LABEL` |
| image size and disk (D95) | 136 MB | 127 MB |
| email and Slack only through Annex | Kanboard has its own SMTP settings | mail settings hidden (`MAIL_CONFIGURATION=false`), no SMTP host, `egress: []`: Kanboard sends no email. Its email notifications and "send by email" don't work, by design |
| no internet unless declared | plugin directory from kanboard.org | `PLUGIN_INSTALLER=false`, `egress: []`. A plugin is unpacked into `/data/plugins/<Name>`; nginx serves only its `Assets/` |
| logging convention: JSON lines on stdout, `request_id`, `user`, `X-Request-Id` | nginx access log off, PHP log in the container | **nginx writes one JSON line per request** (`ts`, `level`, `msg`, `request_id`, `user`, method, path without the query string, status, duration). It reuses the front door's `X-Request-Id` (or makes one with 16 hex characters) and returns it. Health checks, static files and the seed job's own API calls aren't logged. The entrypoint and the jobs log JSON too |
| cross-site requests (D91) | Kanboard checks its own CSRF tokens on forms | **kept on**: nginx refuses a `POST`/`PUT`/`PATCH`/`DELETE` whose `Sec-Fetch-Site` isn't `same-origin`/`none`, or whose `Origin` isn't the app's host. This is the runtime's `csrf_violation`, rewritten for nginx. Kanboard's own forms are same-origin and pass; checked with a real comment form. `ANNEX_CSRF: "off"` would turn it off; it isn't needed |

What Kanboard itself can't meet fully:

- **Logging.** Kanboard's own log lines (`LOG_DRIVER=stderr`, info and above) and PHP's errors are plain text, not
  JSON. They are rare, and Annex stores and searches them all the same. nginx's per-request JSON lines carry the
  request id and the user. Making Kanboard's own lines JSON would take patching Kanboard, so we don't.
- **"GET doesn't change anything."** A few of Kanboard's links change state on a GET (sorting comments, some
  toggles). Each one carries a Kanboard CSRF token, so another site can't trigger it.
- Kanboard's code has a bug: when a comment fails validation, the page fatals (a PHP error in the log, and the
  comment isn't saved). This is upstream and doesn't happen in normal use.

### Sign-in, in detail

- `REVERSE_PROXY_AUTH=true` and `REVERSE_PROXY_USER_HEADER=HTTP_REMOTE_USER`: PHP sees the `Remote-User` header as
  `HTTP_REMOTE_USER`. **Kanboard's default, `REMOTE_USER`, is not the same thing**: it is the HTTP basic-auth user,
  which a client chooses, so it must not be used. Email comes from `HTTP_REMOTE_EMAIL` and the full name from
  `HTTP_REMOTE_NAME`.
- Kanboard 1.2.5x trusts the header only from `TRUSTED_PROXY_NETWORKS`. The entrypoint sets that to the container's
  own networks. Those are internal Docker networks that only Annex's platform containers share; the front door is
  the one that sends people's requests.
- Kanboard's own sign-in is off as far as Kanboard allows:
  - the login form is hidden (`HIDE_LOGIN_FORM`);
  - remember-me is off (`REMEMBER_ME_AUTH=false`);
  - logout is off (`DISABLE_LOGOUT`; it would sign straight back in).
- nginx returns 404 for `/login`, `/forgot-password` and Kanboard's `AuthController` and `PasswordResetController`.
- The schema creates an `admin` user with the password `admin`. The entrypoint disables that account on every start
  while it still has that password (`annex/lockdown.php`).
- The JSON-RPC API (`/jsonrpc.php`) answers only on `127.0.0.1`, which means the container's own jobs; through the
  front door it returns 403.
- On every start, the entrypoint also checks Kanboard's API token in its settings table. If the token is missing, or
  is the fixed one in Kanboard's published schema dump, a new one is made the way Kanboard makes it (30 random
  bytes as hex).

## Jobs

| Job | When | What |
|---|---|---|
| `seed` | by hand: `annex run kanboard seed` (its cron, 30 February, never fires) | reads `venues` and `service_log` through the gateway and creates the content listed below |
| `cronjob` | 06:00 daily | Kanboard's own housekeeping (`cli cronjob`: recurring tasks, daily statistics) |

What `seed` creates, through Kanboard's JSON-RPC API:

- a project per open venue (`opened_on` up to today), "Parts to order", and "Workshop" only if a machine in storage
  has an open ticket, each with the four columns;
- a card per open service ticket (status not `closed`):
  - title: machine and symptom;
  - description: the ticket's details and a link to tech-board;
  - project: the venue's;
  - column: by status (open: Reported, in_progress: Diagnosing, waiting_parts: Waiting for parts);
  - colour: red when the machine is off the floor;
- for a `waiting_parts` ticket, a second card in "Parts to order";
- everyone who has signed in to Kanboard so far becomes a member of these projects.

Cards carry Kanboard's `reference` field: `ticket-<id>`, and `parts-<id>` for the parts cards. A rerun only adds
what's missing (new tickets, a deleted project or column). It never moves or edits a card people have worked on.
The job logs one start line and one end line with counts.

The job calls the API as Kanboard's app user `jsonrpc`, with **Kanboard's own API token**. It reads the token from
Kanboard's settings table through the app's `DATABASE_URL` (`annex/kbdb.php`); it never prints or logs it. So there
is no Annex secret to set and no human step. Kanboard admins see the token under Settings > API. Regenerating it
there is harmless: the next run reads the new one.

### How people get onto the boards

Kanboard creates a person's account on their first visit, as a normal user with no projects. They can't see a
project until they're a member of it.

- At seed time, everyone who has signed in so far is added to the Goat Arcade projects as a project member. A rerun
  of `seed` adds anyone who has signed in since, and creates no cards twice.
- After that, a Kanboard admin (`kaldi-admin`) adds people in Kanboard: Project > Settings > Permissions, per
  project, or once through a Kanboard group given access to the projects.
- Projects people create themselves are private to them, as usual in Kanboard.

## Commands

Nothing on this app is human-only beyond the platform's usual ones (`annex remove`, `rollback --with-data`).

```bash
annex deploy demo/apps/kanboard
annex run kanboard seed        # fills the boards; safe to repeat
```

To make another login a Kanboard admin, change `KANBOARD_DEFAULT_ADMIN` and redeploy. That covers one login; for
more, `kaldi-admin` sets their role in Kanboard (Users).

## Upgrading Kanboard

1. Pick a release that is at least two weeks old, and read its changelog at
   https://github.com/kanboard/kanboard/releases.
2. Get the multi-arch digest: `docker buildx imagetools inspect kanboard/kanboard:<version>` (the index `Digest:`,
   with amd64 and arm64 in its manifests).
3. Set `KANBOARD_VERSION` and `KANBOARD_DIGEST` in the `Dockerfile`.
4. Compare the upstream image's `/etc/nginx/nginx.conf` and `/usr/local/bin/entrypoint.sh` with
   `annex/nginx.conf.tmpl` and `annex/entrypoint.sh`. Check also that `app/constants.php` still reads the settings
   `annex/config.php` defines.
5. Deploy on the local lane first: `ANNEX_ENV=local annex deploy demo/apps/kanboard`. The entrypoint runs
   Kanboard's database migrations (`cli db:migrate`) before the web server starts.

   If the new release fails its health check, Annex switches back. The migrations it ran stay in the database, so
   to go back to the old version, use `annex rollback kanboard --with-data` (human only).
