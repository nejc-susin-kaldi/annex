# AGENTS.md: building an Annex app (static)

This folder is an **Annex app**: a small internal web page that runs behind the client's login on their Annex server. It's plain HTML, CSS and JavaScript in `public/`, served by a tiny server (`server.py`) that also provides the signed-in user and read-only product data. Read the contract with `annex contract` (or the `annex_contract` MCP tool; add a topic, e.g. `annex contract notify`, for email/Slack, webhooks, logging details or another stack).

## What you may change
- Everything in `public/`. `index.html` is the entry page; add CSS, JS, images and more pages as you like.
- Browser libraries can be loaded from a public CDN with a pinned version, e.g. `https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js`.
- `annex.yaml`: `description` and `data.views`.

## How the page gets its data
All requests are relative to the page and go to this app's own server:
- `fetch("api/me")` returns `{login, name, email, groups}` for the signed-in user. Use it for greetings. Never build a login form.
- `fetch("api/data/<view>?<filters>")` returns a JSON array of rows from a gateway view. It only works for views listed under `data.views` in `annex.yaml`; others return 403. Filters use PostgREST syntax:
  - `?limit=10`
  - `?order=aum_eur.desc`
  - `?segment=eq.Private`
  - `?client_name=ilike.*novak*`
  - `?select=month_end,market_value_eur`
  - `?month_end=eq.2026-09-30`
- Views and their columns are in the contract (`annex contract`). All data is synthetic. Large views (e.g. `monthly_positions`, ~40k rows) should be filtered or limited.
- Use relative URLs (`api/...`, not `/api/...` or a hostname), so the page works on any host.

## Logging
`server.py` logs every request as one JSON line (`ts`, `level`, `msg`, `request_id`, `user`, method, path, status, ms) and returns an `X-Request-Id` header, so leave it alone. There is nothing to log in the page itself: never `console.log` secrets, tokens or product records either. Fix what the server's `warn` and `error` lines say (`annex logs <app>`).

## Errors
Annex groups errors for you, and a **new kind of error** emails the owner once. In a static app that means JavaScript errors in the browser: keep `<script src="annex-errors.js"></script>` in every page (it reports uncaught errors and unhandled promise rejections to `server.py`, which logs them as `source=browser` errors; the file is `public/annex-errors.js`, don't edit it). Don't swallow errors with an empty `catch`: let them surface, or show the user a message and rethrow.

## What you must not do
- **Don't change `server.py`, the `Dockerfile` or `.dockerignore`.** No build step runs on the server. If you use a framework with a build step, build locally and put the output in `public/`.
- **No login code, no credentials, no direct database or gateway calls from the browser.**
- **No secrets in `public/`.** Everything there is downloadable by every signed-in user.
- **No `storage` or `schedule`.** For those, use the python-web or python-job template instead.

## Workflow
1. Edit `public/`.
2. `annex deploy .` (or the `annex_deploy` MCP tool with the folder's absolute path). Read any error, fix exactly what it names, and deploy again.
3. Open the URL it prints. Check `annex status <name>` and `annex logs <name>`; the server logs each request.
