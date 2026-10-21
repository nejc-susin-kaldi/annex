# Hardening plan: fixes from the technical review of 21 Oct 2026

For an **orchestrator agent** (Opus) that delegates one step per subagent (Sonnet unless a step says Opus) and integrates the results. Written 21 Oct 2026 against commit `a4964d2` (branch `goat-arcade`; D1–D78 done). The review it comes from is summarised in "What was found"; every step cites the file and line the finding was verified at.

**Read first:** `CLAUDE.md` (ground rules; it overrides everything), `docs/brief.md`, `docs/decisions.md` (D1, D2, D16–D19, D30–D39, D46, D51–D58, D63–D65, D69–D73, D76), `docs/operations.md`, and this whole file. Every subagent gets the same reading list plus its one step.

## What was found (the short version)
The platform's shape is sound and stays: one front door with entrypoint-level header stripping and forward-auth, per-app Postgres roles behind one allowlisted gateway, a pure alert engine, idempotent VM commands, the `annex-selftest` fixture. The weak spots are concentrated:

| Area | Finding | Verified at |
|---|---|---|
| Security | An image `LABEL` is inherited by the container, so any Dockerfile can add Traefik, Ofelia, Homepage or backup labels: a fake login page on the `auth` host (bypass), an unauthenticated route on the `hooks` host, admins' identity headers on the `console` host, `job-exec` as root. The deploy inspects only `.Config.User`. | `cli/src/annex_cli/ops.py:288` |
| Security | App names reach `bash -c` unquoted on the status, logs, run, secret and remove paths; only `init` and the VM script validate. A prompt-injected agent calling `annex_status` with a crafted name runs commands on the VM. | `cli/src/annex_cli/ops.py:228`, `:255`, `:649`, `:691`, `:780`, `:836`, `:855`, `:876` |
| Security | One flat `annex-edge` network: any app can call another app directly with a forged `Remote-User`, and the contract tells apps to trust it. Only the Traefik path is tested. | `cli/src/annex_cli/ops.py:170`, `runtime/annex_runtime.py:33-41` |
| Security | Small: gateway JWT has no `exp`; Homepage config mounted read-write and on `socket-ro`; `owner` is free text but is the console's authorisation key; demo passwords on a `docker run` command line; no CSRF guidance in the contract. | `vm/annex_vm.py:102-111`, `compose.yaml:119`, `cli/src/annex_cli/manifest.py:131`, `vm/bootstrap-secrets.sh:94` |
| Reliability | The gateway audit pass re-aggregates 48 hours of `logs` every 60 s with no cursor and no index on `container`; the usage view does distinct counts over 30 days of raw rows and the tiles read it per app per refresh. appdata-db is the first thing to peg. | `ops/annex_ops/audit.py:121-131`, `db/appdata/annex_ops.sql:122-146`, `ops/annex_ops/usage.py:344-357` |
| Reliability | Every ops call opens a fresh connection; `max_connections=60` is shared with the console, the receiver, Metabase and every app's pool. Ceiling about 8–10 apps with pools of 5. | `ops/annex_ops/db.py:10-16`, `compose.yaml:189-213` |
| Reliability | `/healthz` runs only `SELECT 1`; a follower that hits a `psycopg.Error` outside `OpsError` dies silently and is never restarted. | `ops/annex_ops/app.py:88-95`, `ops/annex_ops/follow_logs.py:71-83`, `ops/annex_ops/logship.py:479-481` |
| Reliability | Buffers are counted in rows, not bytes: the log sink keeps 20,000 rows of up to 16 KB each, the usage ingest re-queues with no cap. A long database outage OOM-kills the 128 MiB ops container. `Sink.lost` is never surfaced. | `ops/annex_ops/logstore.py:23,161-183`, `ops/annex_ops/usage.py:190-192` |
| Reliability | Alert sends run synchronously in the engine thread (10 s SMTP, 10 s Slack, 10 s log tail, no retry); crash counters and grace timers live only in memory; wall clock only. A Traefik outage opens two incidents. A failed `inspect` during reconcile looks like a `Destroy` and deletes the follower's cursor. | `ops/annex_ops/incidents.py`, `ops/annex_ops/engine.py:328-340,473,507`, `ops/annex_ops/watch_docker.py:76-86`, `ops/annex_ops/logship.py:499-510` |
| Reliability | The nightly archive has no `secrets/` or `state/directory`: a bare-VM restore can't sign anyone in or verify a webhook. | `compose.yaml` backup service, `db/appdata/hooks/dump-all.sh` |
| Deploy | No lock and no transaction: two deploys of one app compute the same `r<n>`; an SSH drop between `compose up` and the release record leaves a running container with no record, and the next deploy reuses the tag. | `cli/src/annex_cli/ops.py:447-487` |
| Deploy | `push.sh` is `rsync --delete` from whoever's checkout with no version stamp; shell scripts read the local `.build` env while the CLI reads the VM's copy; `annex secret set` takes values in argv. | `scripts/push.sh:11`, `scripts/lib.sh:17`, `cli/src/annex_cli/main.py:166-168` |
| Deploy | Any Dockerfile builds on the VM unconstrained: no free-disk check, no image size cap, cleanup errors ignored, no `pids_limit` on app containers. | `cli/src/annex_cli/ops.py:280-307,419-421` |
| Maintainability | `annex_ops.sql` is append-only with no version table: a column change has no path. | `db/appdata/annex_ops.sql`, `scripts/push.sh:20` |
| Maintainability | The contract is in six places with no version: `contract.md`, a byte copy in `docs/app-contract.md`, `SKILL.md`, four `AGENTS.md`; the runtime helpers carry no version. | `skills/annex/SKILL.md:44-61`, `runtime/annex_runtime.py:1` |
| Maintainability | `decisions.md` is the only architecture document; superseded entries aren't marked; `## D74` appears twice; the brief's "How the pieces run" is wrong since D1. | `docs/decisions.md:514,522`, `docs/brief.md:89-90` |
| Maintainability | `annex_vm.py` has no test seam: only pure helpers are tested; most SQL is f-string built from validated names where the releases path already uses `dollar_quote`. | `vm/annex_vm.py:152-160,186-195,574-580` |

## How the orchestrator works

1. **Branch.** Ask Nejc which branch to build on (`goat-arcade` or `main` after it merges), then create `hardening` from it. Every step is a commit on that branch, with the step's name in the subject.
2. **Worktrees write, the main checkout pushes.** A subagent works in its own worktree and runs **only unit tests** there (`.env` is gitignored, so a worktree can't talk to the VM anyway). The orchestrator merges the step into `hardening` in the main checkout, runs `scripts/push.sh`, `scripts/test-smoke.sh` and the step's suite, and only then starts the next step that touches the VM. **Never push from a worktree**: `push.sh` rsyncs with `--delete` and would revert every other step's platform files.
3. **One VM lane.** The VM is one shared environment. Steps marked *code-only* may run in parallel worktrees; steps marked *VM* are integrated one at a time, in the order of the table below.
4. **Tests.** The subagent runs the unit tests it touched; the orchestrator runs `test-smoke.sh` after every push and the step's area suite. **Ask Nejc before `scripts/test-all.sh` or `scripts/test-alerts.sh`** (CLAUDE.md). Plan on one `test-all.sh --slow` at the very end, with his go-ahead.
5. **Approval gates.** A step marked **ask Nejc** changes the contract, the network topology, what a backup contains or adds a Python dependency. The orchestrator asks before delegating that step, with the step's text as the question, and records the answer in the decision entry.
6. **Decisions.** Each step adds one entry to `docs/decisions.md`, numbered from **D79**, in the house format (what, why, alternatives). Steps that revise an earlier decision say which one.
7. **Report.** The subagent's hand-back names the files changed, the tests run with their counts, anything it couldn't do, and the decision entry's number. The orchestrator checks the diff before merging; a subagent's report is not proof.
8. **Don't touch:** `scripts/wipe-vm.sh`, `scripts/reset-demo.sh`, the app `vibe-leads`, `dronky`, `democlient/` data, VM system config. Secrets never land on the Mac or in the repo.
9. **Prod flags.** A line starting **Prod:** inside a step means the step's fix is the interim one for the prototype and names what replaces it in production. Those lines are collected in the last section, which is the list to hand to whoever plans the AWS move.

## Order of work

| Step | Delivers | Model | Lane | Depends on | Gate |
|---|---|---|---|---|---|
| A1 | App names validated in every CLI and MCP entry point | Sonnet | VM | — | — |
| A2 | Images with routing, scheduler or backup labels are refused | Sonnet | VM | A1 | — |
| A3 | Small security items: JWT expiry and rotation, Homepage, `owner`, demo passwords, `template1` | Sonnet | VM | A2 | — |
| A4 | CSRF rule in the contract and the templates | Sonnet | VM | — | ask Nejc (contract) |
| A5 | Per-app networks | **Opus** | VM | A2 | ask Nejc (topology) |
| B1 | ops self-status: thread liveness in `/healthz`, follower restart, lost counters surfaced | Sonnet | VM | — | — |
| B2 | Byte budgets on every buffer; bounded queues | Sonnet | code-only, then VM | B1 | — |
| B3 | Gateway audit scan with a cursor; usage rollup table; `logs (container, ts)` index | Sonnet | VM | — | — |
| B4 | Connection reuse in ops; appdata-db sized to its `max_connections`; per-app pool budget | Sonnet | VM | — | ask Nejc (contract line, `psycopg-pool`) |
| B5 | Alert engine: sender thread with retries, persisted counters, monotonic time, one incident per Traefik outage, safe reconcile | **Opus** | VM | B1 | ask Nejc (`test-alerts.sh`) |
| B6 | Log shipper: rate cap before parse; sink inserts off the follower thread | Sonnet | VM | B2 | — |
| B7 | Backups include `secrets/` and `state/directory`; dump marker and staleness alert | Sonnet | VM | — | ask Nejc (archive contents) |
| C1 | Deploy lock and pending release record; interrupted deploys are visible and recoverable | **Opus** | VM | A1 | — |
| C2 | Push version stamp; one source of settings; `secret set` from stdin; prerequisites list | Sonnet | VM | — | — |
| C3 | Numbered schema migrations with a version table | Sonnet | VM | B3, B5 (they add tables; land them as migrations 002+) | — |
| C4 | Contract and runtime version; one source for the agent-facing copies | Sonnet | VM | A4 | — |
| C5 | Build limits: free-disk check, image size cap, `pids_limit`, cleanup warnings | Sonnet | VM | C1 | — |
| C6 | `annex_vm.py` test seam, `dollar_quote` everywhere, provision tests | Sonnet | code-only | C1 | — |
| C7 | `docs/architecture.md`, decisions index with status, brief and CLAUDE.md pointers | Sonnet | code-only | everything else merged | — |
| D | Final run: `test-all.sh --slow`, resource record, teardown and operations docs updated | orchestrator | VM | all | ask Nejc |

Suggested integration order on the VM lane: A1, A2, A3, B1, B3, B4, C2, B2, B6, B7, C1, C5, A4, C4, A5, B5, C3, then C6 and C7, then D. A3/B1/B3/B4/C2 can be written in parallel worktrees while A1 and A2 integrate.

---

## A1. App names validated at every entry point (Sonnet, VM)
**Goal:** no app name reaches a shell unless it matches `NAME_RE`; every public `ops.*` function refuses a bad name with the same fix-it message `init` uses.
**Do.**
- In `cli/src/annex_cli/ops.py`, make `container()` and `project()` (lines 47–53) raise `AnnexError` for a name that fails `mf.NAME_RE` or is in `mf.RESERVED_NAMES`; route every public function (`status`, `logs`, `run_job`, `secret_set`, `remove`, `releases`, `rollback`, `usage`, `sends`, and the helpers `_record`, `_releases`, `_append_release`, `_images`, `_containers`, `_maintenance`) through a single `_name(app)` check at the top. `job` names in `run_job` and `KEY` in `secret_set` get the same treatment with their own regexes (the manifest already has them).
- `shlex.quote` every path built from `r.data` in `r.run(f"…")` calls, or add `Remote.path(*parts)` that does it.
- `mcp/src/annex_mcp/server.py`: nothing to add if `ops` refuses, but confirm the error reaches the agent as text, not a traceback.
**Tests.** `cli/tests/test_ops_names.py`: each public function with `"x; id"`, `"../etc"`, `"Console"` raises `AnnexError` **before** any `Remote` call (patch `Remote.run` to fail the test if called). `test-smoke.sh` unchanged. Suite: `scripts/test-apps.sh`.
**Done when** the unit test passes, `./annex status 'x; id'` prints the fix-it message, and `test-smoke.sh` is green. Decision entry: one paragraph under D79.

## A2. Refuse images that carry platform labels (Sonnet, VM)
**Goal:** a Dockerfile can't add routes, jobs, tiles or backup hooks.
**Do.**
- In `_build` (`ops.py:280-307`), after the user check, read `docker image inspect -f '{{json .Config.Labels}}'`. Refuse (and `rmi`) an image with any label whose key starts with `traefik.`, `ofelia.`, `homepage.`, `docker-volume-backup.` or `com.kaldi.annex` (the two the build itself adds, `com.kaldi.annex` and `com.kaldi.annex.app`, are container labels set by `--label`, not image labels; confirm by inspecting a template build, and if they do appear on the image, allow exactly those two values). The message names the offending label and says labels are Annex's job.
- In `compose_for` (`ops.py:149-224`), set `traefik.enable: "false"` on job-only apps and `ofelia.enabled: "false"` on apps without a schedule, so an inherited `true` can't win if the inspect is ever bypassed.
- Check Ofelia 0.3.22's label handling for `job-run` on non-Ofelia containers (its source is on GitHub; `mcuadros/ofelia`). Record what it does in the decision entry; if `job-run` with a `volume` is honoured, say so, because then the inspect check is the only thing between a builder and root on the VM.
**Tests.** `scripts/selftest.py` gets a variant flag that adds a `LABEL traefik.http.routers.evil.rule=…` to the Dockerfile; `scripts/test-platform.sh` deploys it and expects the refusal text, then deploys the clean fixture. Unit test for the label filter (pure function, list in, verdict out).
**Done when** the suite passes and a Dockerfile with a Traefik label can't be deployed. Decision entry.

## A3. Small security items (Sonnet, VM)
**Do, each one a small commit:**
1. **JWT expiry and rotation.** `vm/annex_vm.py:102-111`: add `exp` (one year) and regenerate the gateway token on every `provision` instead of only when missing (line 162); the container is recreated on every deploy anyway, and a rollback re-provisions. Contract text: "the gateway token changes on every deploy; never store it". **Prod:** short-lived tokens minted per request by a deploy API, and `GATEWAY_JWT_SECRET` rotation.
2. **Homepage.** Mount `./config/homepage` read-only (`compose.yaml:119`); Homepage writes nothing it needs. Replace the `server: annex` Docker widgets in `config/homepage/services.yaml.tmpl` with the ops `customapi` tiles that already exist (D46, D73), then drop Homepage from `socket-ro` and delete `config/homepage/docker.yaml`. If a tile can't be replaced, keep the provider and say why in the decision.
3. **`owner`.** `cli/src/annex_cli/manifest.py:131`: `owner` must match `LOGIN_RE`; `description` is capped at 200 characters and may not contain control characters. Message says why (the owner is a login, it decides who sees the app in the console).
4. **Demo passwords.** `vm/bootstrap-secrets.sh:94`: use Authelia's `--random` as `annex_vm.py:770` does, and keep writing the plain demo passwords where `scripts/show-users.sh` reads them. `scripts/push.sh:20`: pass the four passwords through `PGPASSWORD`-style environment or a `\set` read from a file rather than `-v` on the command line.
5. **`template1`.** `db/appdata/init`: `REVOKE CONNECT ON DATABASE template1 FROM PUBLIC`.
**Tests.** `test-smoke.sh` and `scripts/test-users.sh`; the Homepage tile check in `scripts/test-console.sh`.
**Done when** all five are in, with one decision entry listing them.

## A4. CSRF rule in the contract and the templates (Sonnet, VM, **ask Nejc**: contract change)
**Goal:** one session cookie serves every app subdomain, so a state-changing request from one app's page to another app is sent with the user's identity. The contract says nothing.
**Do.** Add a "Cross-site requests" paragraph to `cli/src/annex_cli/contract.md`: state-changing endpoints must reject requests whose `Sec-Fetch-Site` is not `same-origin` or `none`, or whose `Origin` doesn't match the app's host. Put that check in `runtime/annex_runtime.py` and `.js` as a one-line middleware the templates enable by default (`python-web`, `node-web`; `static` has no state), with an opt-out env for apps that are called by other apps on purpose. Run `scripts/sync-runtime.sh`. `annex contract` topic map in `ops.py` (`CONTRACT_TOPICS`): the section is core.
**Tests.** `runtime/tests` for both runtimes; `scripts/test-apps.sh` posts to the selftest fixture with a foreign `Origin` and expects 403.
**Prod:** per-app session cookies or an origin check in Traefik, once the IdP is decided.

## A5. Per-app networks (**Opus**, VM, **ask Nejc**: topology change)
**Goal:** no app can reach another app's port. Identity headers can only arrive from Traefik.
**Spike first** (report before building): which platform containers must reach an app? Traefik (routes), ops (health checks `health.py:45-58`, hook delivery `hooks_delivery.py`, log drain is via Docker not network), the gateway and ops are reached *by* apps. Design: each app gets network `annex-app-<name>` (internal: false is not needed; the app reaches the internet through `annex-edge` today, decide whether apps keep `annex-edge` for egress with no other app on it, or whether egress moves to a dedicated `annex-egress` network that holds only apps that declare `egress`). Platform containers join an app's network on deploy with `docker network connect` and leave on remove; `push.sh` reconnects them after a recreate (`annex_vm.py networks-sync`, idempotent, reading the app list from `/srv/annex/deploy`). Traefik's `traefik.docker.network` label on each app points at its own network. Check Docker's limit on networks per container and the VM's bridge count for 20–30 apps.
**Do** after Nejc approves the design: `compose_for`, `remove`, `push.sh`, `annex_vm.py`, `compose.yaml` comments, `docs/operations.md`. The contract note "identity headers are trustworthy only on the Traefik path" goes in regardless.
**Tests.** `scripts/test-platform.sh`: from the selftest container, `curl http://annex-app-<other>:8080/` must fail to connect; the console's existing "no network path from an app" check stays green. `scripts/test-hooks.sh`, `test-alerts.sh` scenarios that rely on ops reaching apps (ask before the long one; `test-smoke.sh` covers health).
**Done when** app-to-app is refused by the network, every suite that ran before is green, and the decision entry revises D2 and D36.

## B1. ops self-status (Sonnet, VM)
**Goal:** a dead thread is a 503, not a silence.
**Do.**
- A small `threads.py` registry: every long-running thread (`incidents.py:318-367`, `logship.py:493-497`, `logstore.py:198`, the health checker, the host sampler, the auditor, the error scanner, the usage rollup, the hooks deliverer) registers a name and bumps a `last_progress` timestamp on every loop iteration. `/healthz` (`app.py:88-95`) reports each thread's `is_alive()` and age and returns 503 when one is dead or older than its expected period × 3. Compose healthcheck unchanged (it already hits `/healthz`), so Docker marks ops unhealthy; add `autoheal`-style restart? No: `restart: unless-stopped` ignores health. Instead the Docker watcher's own `unhealthy` signal for `platform:ops` can't come from ops itself, so make the **console's** Platform page and `ctl health` show the thread table, and have the engine raise `platform:ops` from the health checker's point of view (the checker already requests the front door; add `http://ops:8000/healthz` as a platform check).
- `follow_logs.Follower.run` (`:71-83`): catch `Exception` around the whole iteration, log once a minute, back off and retry; never exit. The shipper (`logship.py:479-481`) re-checks `is_alive()` on its tick and restarts a dead follower from its cursor.
- Surface `Sink.lost` (`logstore.py:167,183`), the usage ingest's dropped count (after B2) and queue depths in `/healthz` and `ctl logs-stats`.
**Tests.** `ops/tests/test_threads.py` (registry, staleness verdict, pure); `test_follow_exact.py` gets a case where `store.get` raises a non-`OpsError` and the thread survives. `scripts/test-logs.sh` already restarts ops; add a check that `/healthz` lists the followers.
**Prod:** metrics (Prometheus or CloudWatch) replace the thread table; `/healthz` stays.

## B2. Byte budgets (Sonnet, code-only, then VM)
**Do.** `logstore.Sink` (`:23,161-183`): budget in bytes (`len(msg) + len(serialised fields)`, default 32 MiB) as well as rows; drop oldest and count. `usage.Ingest.flush` (`:190-192`): cap the retry buffer (default 50,000 rows or 16 MiB), drop oldest, count. `incidents.py:29` and `logship.py:446` queues: `maxsize` with a drop-and-count on `Full` for signals that are safe to lose (log lines, usage rows), never for Docker events. Document the numbers in `config.py` with the existing tuning mechanism.
**Tests.** Unit tests for each budget. On the VM: `scripts/test-logs.sh`.

## B3. Gateway audit cursor and usage rollup (Sonnet, VM)
**Do.**
- `audit.scan_gateway` (`audit.py:121-131`): keep a cursor like `scan_signins` (the `CursorStore` over `log_cursors`, key `audit:gateway`); aggregate only buckets that closed since the cursor, at most once per bucket, with a one-bucket replay for late rows; the first run after upgrade starts from the newest existing gateway audit event.
- `CREATE INDEX IF NOT EXISTS logs_container_ts ON annex_ops.logs (container, ts DESC)` (a parent index on the partitioned table; check it propagates to existing partitions and that `logstore` creates new partitions with it).
- Usage: a table `annex_ops.usage_rollup_apps` written by the existing 10-minute `usage.rollup`, with the columns `usage_apps` has today; `tile_for` (`usage.py:344-357`), `app_status` and the console read the table. Keep the view for one release so nothing breaks, then drop it in C3's migration.
- New tables only; nothing existing changes (CLAUDE.md rule, until C3).
**Tests.** `ops/tests/test_audit.py` for the cursor logic; `scripts/test-audit.sh`, `scripts/test-usage.sh` (numbers equal to the CLI's).

## B4. Connections (Sonnet, VM, **ask Nejc**: a pinned `psycopg-pool` dependency and one contract line)
**Do.** `ops/annex_ops/db.py`: a `psycopg_pool.ConnectionPool` (min 1, max 4) with `connect()` returning a pooled connection as a context manager, same `OpsError` mapping; the console and receiver use the same module with max 2. `compose.yaml`: either raise appdata-db's `mem_limit` to fit `max_connections=60` (about 60 × 6 MB + 128 MB shared buffers + headroom, so 640m) or lower `max_connections` to 40 and keep 384m; pick by the VM's budget (brief: platform under 2.5 GiB) and record it. Contract (`storage` section): "use at most 5 connections; a pool of 2–5 is right". Log the per-database connection count on the console's Platform page from `pg_stat_activity` (the console role needs `SELECT` on a small view; new view, fine).
**Tests.** `ops/tests` run as before; `test-smoke.sh`; `scripts/test-notify.sh`.

## B5. Alert engine robustness (**Opus**, VM, **ask Nejc** before `scripts/test-alerts.sh`)
**Do.**
- Sends off the engine thread: `incidents.notify` hands messages to a sender thread with a persistent retry queue (`annex_ops.outbox` table: message, attempts, next_attempt_at; the receiver's `hook_events` is the pattern). The engine stays pure. A send that fails three times is logged and shown on the console's Platform page.
- Persist crash counters and timers (`dies`, `stopped_since`, `last_die`, `mem_low_since`, `hooks_failing`) in a `container_state` table written on change, loaded at start, so an ops restart doesn't reset them; document what is still memory-only.
- Time: the engine takes `now` from a monotonic source for durations and wall time only for display; an NTP step backwards can't freeze reminders.
- One incident per Traefik outage: when `platform:traefik` is down, the checker's `platform:front-door` signal is suppressed (or merged into the same subject); decide in the engine with a test.
- Reconcile safety: `watch_docker.py:76-86`: if any `inspect` failed, don't emit `ReconcileDone` (keep `reconcile=True`, retry next pass); `logship.py:499-510` deletes a cursor only on a real `Destroy` event.
**Tests.** `ops/tests/test_engine.py` additions (every new rule); `test_sender.py`; `scripts/test-alerts.sh` once, with Nejc's go-ahead, plus `test-host.sh`.
**Prod:** the outbox is also where a real email provider's bounces would be recorded.

## B6. Log shipper ordering (Sonnet, VM)
**Do.** `logship.py:336-353`: call `cap.admit(ns)` before `spec.parse(raw)` (the cap only needs `ns`); keep the dropped-lines row. `logstore.Sink.add` (`:161-170`) only enqueues; the sink thread does every insert, so a slow `INSERT` never stalls a follower, and `flush()` before a cursor save waits on the sink with a timeout rather than inserting inline.
**Tests.** `ops/tests/test_logship.py`, `test_logstore.py`; `scripts/test-logs.sh` (rate cap, restart without gap, drain).

## B7. Backups that restore a VM (Sonnet, VM, **ask Nejc**: the archive then contains secrets)
**Do.** Add `${ANNEX_DATA_DIR}/secrets:/backup/secrets:ro` and `state/directory` to the backup container's volumes; the archive stays under `/srv/annex/backups`, mode 600, owned by `annex`. `dump-all.sh` writes `dumps/current/MARKER` with its timestamp; `host.py` compares the newest archive's embedded marker date with the archive date and the engine raises `backup-stale` when they differ by more than a day (new signal, engine test). `docs/operations.md`: a "restore a whole VM" recipe, tried once on the VM against a scratch directory (not a wipe).
**Prod:** archives to S3 with offen's GPG encryption and a key outside the VM; secrets in a secrets manager make this moot for most of `secrets/`.

## C1. Deploy lock and pending record (**Opus**, VM)
**Goal:** two deploys can't race; an interrupted deploy is visible and the next one recovers.
**Do.**
- `annex_vm.py deploy-begin <app>`: takes `/srv/annex/deploy/<app>/.lock` atomically (`mkdir`, with owner, pid-less token, started_at; stale after 30 minutes with a message that names it), computes the next release number from `releases.jsonl`, appends `{"release": n, "result": "pending", …}` and returns `n`. `deploy-end <app> <n> live|failed <json>` rewrites the pending line (the file is small: rewrite atomically) and releases the lock. The CLI (`ops.py:431-500`) calls `deploy-begin` right after validation and `deploy-end` on every exit path (`try/finally`); `rollback` uses the same pair.
- A deploy that finds a `pending` line from an earlier run reports it ("r5 was interrupted at <step>; the container that is running is r4 unless `docker ps` says otherwise") and reconciles: if the pending image's container is running and healthy, mark it live; else remove the container and mark it failed. Write this as `annex_vm.py deploy-reconcile`, called by `deploy-begin`.
- Order inside the sequence: build, snapshot, **begin**, provision, switch, gate, **end**. Provisioning still runs before the switch (grants must exist when the new container starts); note in the decision that a failed gate re-provisions the old manifest, as today.
- `remote.py:31`: catch `subprocess.TimeoutExpired` and turn it into an `AnnexError` that says the step and that the lock will expire.
**Tests.** `vm/tests/test_deploy_lock.py` (lock, stale lock, pending rewrite; pure file operations in a temp dir); `scripts/test-rollback.sh` extended with: two concurrent deploys of the selftest fixture, one wins, the other says who holds the lock; a deploy killed with `SIGKILL` mid-gate, then a deploy that reconciles.
**Prod:** the lock and the pending record live on in the deploy API; the API is what replaces the shared SSH key (see the last section).

## C2. Push stamp, one settings source, `secret set` from stdin, prerequisites (Sonnet, VM)
**Do.**
- `scripts/render.py` writes `.build/platform/VERSION` (`git rev-parse HEAD`, dirty flag, sha256 of `.env`); `push.sh` reads the VM's `platform/VERSION` first and refuses when the VM's commit is not an ancestor of the local one, unless `--force`; prints both.
- `scripts/lib.sh` `setting()` reads `platform/.env` **on the VM** (one `ssh cat`, cached per run) so scripts and the CLI agree; `render` still writes the local copy for the rsync.
- `annex secret set <app> KEY` reads the value from stdin (or `--from-file`); `KEY=VALUE` still works but warns that the value is in shell history. MCP unchanged (human-only).
- `docs/setup.md`: a "Prerequisites" section listing Mac tools (uv, Python ≥3.12, mkcert, rsync, node for `test-smoke.sh`, Docker only for the optional context) and VM tools (jq, python3-yaml), and the absolute paths at lines 90 and 103 replaced by `$(pwd)`.
**Tests.** `test-smoke.sh`; a unit test for the ancestor check (pure); `scripts/test-notify.sh` (uses `secret set`).

## C3. Schema migrations (Sonnet, VM)
**Do.** `db/appdata/migrations/001_base.sql` is today's `annex_ops.sql` unchanged (idempotent, so it applies cleanly on the existing database); `002_…` onwards are the tables B3, B5 and B7 added (move them out of `001` into their own files). `annex_ops.schema_version(version, applied_at)`; `scripts/push.sh` applies pending files in order, each in one transaction (`psql --single-transaction`), stops at the first failure and says which. Grants stay at the end of each file that needs them. Update CLAUDE.md's "it can't change an existing table" rule to "a change to an existing table is a new migration file". Delete `annex_ops.sql` once the migrations apply on the VM.
**Tests.** `test-smoke.sh`; `push.sh` twice in a row is a no-op the second time; a deliberate failing migration in a scratch file stops the push with the name (then remove it).
**Prod:** the same mechanism; add a `migrate --dry-run` before a platform upgrade.

## C4. Contract and runtime version, one source for the copies (Sonnet, VM)
**Do.** `ANNEX_CONTRACT_VERSION: 1` at the top of `cli/src/annex_cli/contract.md`; `__version__` in `runtime/annex_runtime.py` and `.js`; the deploy records both in `releases.jsonl` and the warning at `ops.py:506-511` says "your runtime is v1, the current one is v2" instead of "differs". `scripts/sync-contract.sh` (next to `sync-runtime.sh`) copies `contract.md` to `docs/app-contract.md` and regenerates the contract summary between markers in `skills/annex/SKILL.md` and each `templates/*/AGENTS.md`; `--check` runs in `test-all.sh`. The MCP's `annex_logs` gets the `source` option it lacks (`server.py:97-102`).
**Tests.** `cli/tests` for the version comparison; `sync-contract.sh --check` green; `test-smoke.sh`.

## C5. Build limits (Sonnet, VM)
**Do.** Before `docker build`: refuse when `df` on `/srv/annex` has under 3 GB free, with the message naming `docker system df`. After the build: `docker image inspect -f '{{.Size}}'`; refuse (and `rmi`) above 2 GB. `compose_for`: `pids_limit: 256`. `_cleanup` (`ops.py:419-421`): a failure becomes a warning in the deploy result instead of silence. Note that BuildKit ignores `--memory`; a build-time limit needs a builder with limits (prod).
**Tests.** `scripts/test-apps.sh` with a selftest variant whose Dockerfile creates a 3 GB file (or a lower test cap via an env var, so the test is fast).
**Prod:** builds in CI, images pulled from a registry; no build on the client's server.

## C6. `annex_vm.py` test seam (Sonnet, code-only)
**Do.** One module-level `run = subprocess.run` that tests replace with a fake recording the commands; `cmd_provision`, `cmd_snapshot`, `cmd_restore`, `cmd_release_record` get tests in `vm/tests` with a fake Docker/psql. Replace the f-string SQL at `:152-160`, `:186-195`, `:227-232`, `:238` with the `dollar_quote` pattern or `format('%I')` for identifiers. Stdlib stays.
**Tests.** `python3 -m unittest discover -s vm/tests`.

## C7. Documentation (Sonnet, code-only, after everything merges)
**Do.** `docs/architecture.md`: the current system in one page (containers, networks, who may reach what, the files per app on the VM, the deploy sequence after C1, the ops threads after B1), with the diagram from the brief redrawn. `docs/decisions.md`: fix the duplicated `## D74` (`:514,522`; renumber the second as D74b and leave the references), add a header table "index with status" (active, superseded by Dn, retired), mark D44 and D46's Metabase part as superseded. `docs/brief.md`: a one-line note at "How the pieces run" pointing to D1 and architecture.md (the brief is historical; don't rewrite it). `CLAUDE.md`: point "Layout" at architecture.md. `docs/operations.md`: the new commands (`deploy-reconcile`, `networks-sync`, migrations, `secret set` from stdin).

## D. Final verification (orchestrator, **ask Nejc**)
With Nejc's go-ahead: `scripts/test-all.sh --slow`, output to a file (not piped). Record `docker stats` and `df -h /` as a resource record in `decisions.md`. Update `docs/teardown.md` for anything the steps added. Report to Nejc: suites and counts, anything red, the decision numbers, and the production list below with any changes the work turned up.

---

## Production changes: stays as is now, changes before a client runs it in AWS

These are deliberately **not** in the steps above. Each names the prototype state, why it is accepted now, and what replaces it. The owner of the AWS move takes this list.

1. **Builder = root.** One shared SSH key with passwordless sudo and the Docker socket; the audit actor is a client-side string (`ops.py:236`). Accepted because the VM is disposable (brief). **Prod:** a deploy API on the server, bound to the IdP identity, that owns the lock (C1), the pending record, provisioning and the switch; the CLI and MCP talk to it; `annex_vm.py` is its only privileged part. Human-only commands become server-side authorisation, not CLI convention (D58).
2. **Identity: Authelia file backend.** Accepted as the no-SSO mode (D57). **Prod:** OIDC against the client's IdP (Authentik, Cognito, Entra); keep the `Remote-*` header contract; `annex users` becomes read-only or goes.
3. **Front door: mkcert and the SSH tunnel.** **Prod:** ALB or CloudFront with ACM; `ANNEX_TLS_MODE=upstream` exists (`render.py`), untested against a real proxy.
4. **Secrets on disk** (`/srv/annex/secrets`, `master.env`, hook secrets in clear in `secrets/hooks/<app>.json`). **Prod:** a secrets manager; rotation for `GATEWAY_JWT_SECRET` and per-app tokens; A3's `exp` is the interim.
5. **Builds on the client's server.** **Prod:** CI builds, a registry (ECR), `pull_policy: always`, tag = release; C5's limits are the interim.
6. **Backups on the same disk, unencrypted.** **Prod:** S3 with offen's GPG encryption, key outside the VM; B7 is the interim.
7. **One flat network** if A5 is deferred. **Prod:** per-app networks are a must with several builders.
8. **Ofelia restarts for the whole fleet on any schedule change** (D18, `ops.py:392-396`). **Prod:** check whether a newer pinned Ofelia watches labels; otherwise schedules move into ops (it already has a scheduler thread model) or to the platform's scheduled tasks.
9. **ops observability.** B1's thread table and `/healthz` are the interim. **Prod:** metrics and structured ops logs to CloudWatch or Loki, behind the same `annex logs` interface (M2 plan, decision 1).
10. **Webhook replay** for providers without a timestamp (GitHub, Trello, shared secret). **Prod:** per-event id dedupe window per hook.
11. **Password reset from any LAN host** lands in Mailpit, which admins read. **Prod:** a real SMTP relay; Mailpit goes.
12. **Single VM, small Postgres.** B3 and B4 buy headroom to roughly 20 apps. **Prod:** RDS for appdata or a bigger instance; the console's read-only role and the log table work unchanged.
13. **Session cookie shared by every app subdomain.** A4's check is the interim. **Prod:** per-app cookies or an origin check in the front door.
14. **`decisions.md` as the architecture record.** C7 adds the architecture page and the index. **Prod:** keep the page current per milestone; decisions stay the log.
