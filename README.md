# Kaldi Annex: M0 homelab prototype

A small platform where builders (and their AI coding agents) deploy internal tools behind login onto one client's server. This repo targets a disposable VM, `annex`, on Nejc's homelab. The brief is in [docs/brief.md](docs/brief.md), and decisions are in [docs/decisions.md](docs/decisions.md).

## Quick start (from the Mac)
```bash
cp .env.example .env          # then adjust; ANNEX_PUBLIC_PORT=8443 for tunnel testing
scripts/bootstrap.sh          # VM layout, secrets, demo users, mkcert cert (idempotent)
scripts/push.sh --extras      # sync and start the platform, incl. Metabase and Dozzle
scripts/test-platform.sh      # M0.1 checks through an SSH tunnel
scripts/show-users.sh         # demo user passwords
```
For browser access, see [docs/setup.md](docs/setup.md) §3.

## What runs
| Service | Host | Who |
|---|---|---|
| Homepage (discovery) | `home` | everyone signed in |
| Authelia (login) | `auth` | everyone |
| Metabase (extras) | `metabase` | everyone signed in; its own login (D9) |
| Dozzle (extras) | `logs` | admins |
| PostgREST gateway | `gateway` | apps with a JWT; admins via browser |

Plus product-db and appdata-db (Postgres 16), Ofelia (jobs), offen backup (nightly, 7 days) and two Docker socket proxies. Hostnames come from `ANNEX_HOST_PATTERN`, `{name}.annex.home.arpa` by default.

## Scripts
| Script | Does |
|---|---|
| `scripts/bootstrap.sh` | prepares `/srv/annex`, generates secrets on the VM, pushes the cert in `local` TLS mode |
| `scripts/push.sh [--extras]` | renders config, rsyncs to `/srv/annex/platform`, `docker compose up --wait`, applies gateway views, configures Metabase |
| `scripts/extras.sh on\|off` | Metabase and Dozzle on or off |
| `scripts/tunnel.sh up\|down` | SSH tunnel `127.0.0.1:8443 → annex:443` |
| `scripts/test-platform.sh` | M0.1 acceptance checks |
| `scripts/wipe-vm.sh` | removes everything Annex from the VM (asks first) |
| `scripts/show-users.sh` | prints demo user passwords |
