#!/usr/bin/env python3
"""VM-side app and user operations for the annex CLI.

Runs ON THE VM with the stock python3 (stdlib only; the user commands also use Ubuntu's python3-yaml), called
over SSH by the CLI. Database passwords and gateway tokens are generated and kept here, in
/srv/annex/secrets/apps/<app>/. Every command prints one JSON object; secret values are never printed, with
one exception: `users-add/users-reset --print-password` returns the new password once, for a human.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

DATA = Path(os.environ.get("ANNEX_DATA_DIR", "/srv/annex"))
SECRETS = DATA / "secrets"
PRODUCT_DB = "annex-product-db-1"
APPDATA_DB = "annex-appdata-db-1"
BACKUP = "annex-backup-1"
BACKUPS = DATA / "backups"
SNAPSHOTS = DATA / "snapshots"
SNAPSHOT_MAX_FILES_KB = 200 * 1024
APP_UID = 10001
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CHANNEL_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,79}$")  # same as the CLI's notify.slack and annex-ops
OPS_DB = "annex_ops"
HOOK_VERIFY = ("trello", "stripe", "github", "slack", "shared-secret")
HOOK_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


class Fail(Exception):
    pass


def out(obj: dict) -> None:
    print(json.dumps(obj))


def role_name(app: str) -> str:
    return "app_" + app.replace("-", "_")


def app_secrets_dir(app: str) -> Path:
    d = SECRETS / "apps" / app
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def psql(container: str, db: str, sql: str) -> str:
    """Run SQL as postgres inside a database container (local socket, trust auth)."""
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-qAt", "-U", "postgres", "-d", db],
        input=sql, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise Fail(f"SQL failed in {container}: {r.stderr.strip()}")
    return r.stdout.strip()


def read_kv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k] = v[1:-1] if v.startswith("'") and v.endswith("'") else v
    return env


def write_kv(path: Path, env: dict[str, str]) -> None:
    """Compose env_file format; single quotes keep values literal (no $ interpolation)."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in env.items():
            f.write(f"{k}='{v}'\n")
    tmp.replace(path)


def secret_file(path: Path, nbytes: int = 24) -> str:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(nbytes))
    return path.read_text().strip()


JWT_LIFETIME = 365 * 24 * 3600  # D80: the token is regenerated on every provision, so a year is a ceiling, not a plan


def jwt(role: str, app: str, key: str | None = None, now: int | None = None) -> str:
    if key is None:
        key = read_kv(SECRETS / "master.env")["GATEWAY_JWT_SECRET"]
    now = int(time.time()) if now is None else now

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps({"role": role, "app": app, "iat": now, "exp": now + JWT_LIFETIME}, separators=(",", ":")).encode())
    sig = b64(hmac.new(key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def api_views() -> list[str]:
    rows = psql(PRODUCT_DB, "product",
                "SELECT table_name FROM information_schema.views WHERE table_schema = 'annex_api' ORDER BY 1;")
    return [r for r in rows.splitlines() if r]


def maintenance(app: str, minutes: int | None) -> None:
    """Hold annex-ops's alerts for the app while its container is stopped on purpose (None: release them).
    Best effort: a failure here must never block a restore."""
    args = ["--minutes", str(minutes)] if minutes else ["--end"]
    subprocess.run(["docker", "exec", "annex-ops-1", "python", "-m", "annex_ops.ctl", "maintenance", f"app:{app}", *args, "--json"],
                   capture_output=True)


def helper_image() -> str:
    """An image that is always present (the platform's Postgres) for chown/rm as root."""
    r = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", APPDATA_DB], capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail("appdata-db is not running; start the platform with scripts/push.sh")
    return r.stdout.strip()


def as_root(cmd: list[str], mount: Path) -> None:
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", cmd[0],
                    "-v", f"{mount}:{mount}", helper_image(), *cmd[1:]], check=True, capture_output=True)


# --- product-db: per-app gateway role -------------------------------------
def grant_views(app: str, views: list[str]) -> str | None:
    role = role_name(app)
    if not views:
        drop_product_role(app)
        return None
    known = set(api_views())
    unknown = [v for v in views if v not in known]
    if unknown:
        raise Fail(f"views not in the gateway allowlist: {', '.join(unknown)}; available: {', '.join(sorted(known))}")
    grants = "\n".join(f"GRANT SELECT ON annex_api.{v} TO {role};" for v in views)
    psql(PRODUCT_DB, "product", f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN CREATE ROLE {role} NOLOGIN; END IF;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA annex_api FROM {role};
GRANT USAGE ON SCHEMA annex_api TO {role};
{grants}
GRANT {role} TO authenticator;
""")
    # a new token on every provision (D80); written beside the old one and renamed, so a reader never sees half a file
    token_path = app_secrets_dir(app) / "gateway_token"
    token = jwt(role, app)
    tmp = token_path.with_name(".gateway_token.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    tmp.replace(token_path)
    return token


def drop_product_role(app: str) -> None:
    role = role_name(app)
    psql(PRODUCT_DB, "product", f"""
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
    EXECUTE 'DROP OWNED BY {role}';
    EXECUTE 'DROP ROLE {role}';
  END IF;
END $$;
""")
    (SECRETS / "apps" / app / "gateway_token").unlink(missing_ok=True)


# --- appdata-db: per-app database -----------------------------------------
def ensure_database(app: str) -> str:
    role = role_name(app)
    pw = secret_file(app_secrets_dir(app) / "db_password")
    psql(APPDATA_DB, "postgres", f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN CREATE ROLE {role} LOGIN; END IF;
END $$;
ALTER ROLE {role} WITH LOGIN PASSWORD '{pw}';
SELECT 'CREATE DATABASE {role} OWNER {role}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '{role}')
\\gexec
REVOKE CONNECT ON DATABASE {role} FROM PUBLIC;
GRANT CONNECT ON DATABASE {role} TO {role};
""")
    return f"postgres://{role}:{pw}@appdata-db:5432/{role}"


def drop_database(app: str) -> None:
    role = role_name(app)
    psql(APPDATA_DB, "postgres", f"DROP DATABASE IF EXISTS {role} WITH (FORCE);\nDROP ROLE IF EXISTS {role};")


def database_exists(app: str) -> bool:
    return psql(APPDATA_DB, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{role_name(app)}';") == "1"


# --- annex-ops: what an app's token may do (email, Slack channels) ----------
def ops_db_exists() -> bool:
    return psql(APPDATA_DB, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{OPS_DB}';") == "1"


def grant_notify(app: str, email: bool, channels: list[str]) -> str | None:
    """Create or update the app's annex-ops grant and return its token (kept in secrets/apps/<app>/ops_token;
    only the sha256 goes into the database). Nothing requested: revoke."""
    if not email and not channels:
        drop_notify(app)
        return None
    bad = [c for c in channels if not CHANNEL_RE.match(c)]
    if bad:
        raise Fail(f"notify.slack: {', '.join(bad)} is not a channel name; use '#name' in lowercase, e.g. '#finance-reports'")
    if not ops_db_exists():
        raise Fail("annex-ops isn't set up on this server: run scripts/bootstrap.sh and scripts/push.sh")
    token = secret_file(app_secrets_dir(app) / "ops_token", 32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    chans = "ARRAY[" + ",".join(f"'{c}'" for c in channels) + "]::text[]"
    psql(APPDATA_DB, OPS_DB, f"""
INSERT INTO annex_ops.app_grants (app, token_sha256, email, slack_channels, updated_at)
VALUES ('{app}', '{digest}', {'true' if email else 'false'}, {chans}, now())
ON CONFLICT (app) DO UPDATE SET token_sha256 = EXCLUDED.token_sha256, email = EXCLUDED.email,
  slack_channels = EXCLUDED.slack_channels, updated_at = now();
""")
    return token


def drop_notify(app: str) -> None:
    if ops_db_exists():
        psql(APPDATA_DB, OPS_DB, f"DELETE FROM annex_ops.app_grants WHERE app = '{app}';")
    (SECRETS / "apps" / app / "ops_token").unlink(missing_ok=True)


# --- incoming webhooks: what annex-ops needs to check and forward a provider's call ------------------------
def write_hooks(app: str, hooks: list[dict], port: int, user_secrets: dict[str, str]) -> str | None:
    """Write (or, with no hooks, delete) the app's webhook file for annex-ops: its token for the app, and for
    each hook the way to verify, the signing secret's VALUE (copied from secrets.env, the only place outside
    secrets/apps/<app>) and the app's path. Returns the token the app checks. Never prints a secret."""
    hdir = SECRETS / "hooks"
    path = hdir / f"{app}.json"
    token_path = SECRETS / "apps" / app / "hook_token"
    if not hooks:
        path.unlink(missing_ok=True)
        token_path.unlink(missing_ok=True)
        return None
    hdir.mkdir(mode=0o755, parents=True, exist_ok=True)
    seen = set()
    for h in hooks:
        if not NAME_RE.match(str(h.get("name", ""))) or h["name"] in seen:
            raise Fail(f"webhook name {h.get('name')!r} is not valid or is used twice")
        seen.add(h["name"])
        if h.get("verify") not in HOOK_VERIFY:
            raise Fail(f"webhook {h['name']}: verify must be one of {', '.join(HOOK_VERIFY)}")
        if not HOOK_PATH_RE.match(str(h.get("path", ""))):
            raise Fail(f"webhook {h['name']}: path {h.get('path')!r} must start with '/'")
        if h.get("secret") not in user_secrets:
            raise Fail(f"webhook {h['name']}: its secret {h.get('secret')} is not set. A human sets it with: "
                       f"annex secret set {app} {h.get('secret')}  (it asks for the value; or --from-file PATH)")
    token = secret_file(app_secrets_dir(app) / "hook_token", 32)
    data = {"token": token, "port": port,
            "hooks": {h["name"]: {"verify": h["verify"], "secret": user_secrets[h["secret"]], "path": h["path"]} for h in hooks}}
    tmp = hdir / f".{app}.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    tmp.replace(path)
    # ops runs as uid 10001; the directory stays ours, so the next provision can replace the file
    as_root(["chown", "10001:10001", str(path)], hdir)
    return token


def restore_database(app: str, dump: Path) -> None:
    """Replace the app's database with a pg_dump -Fc file."""
    role = role_name(app)
    psql(APPDATA_DB, "postgres", f"DROP DATABASE IF EXISTS {role} WITH (FORCE);")
    ensure_database(app)
    with open(dump, "rb") as f:
        r = subprocess.run(["docker", "exec", "-i", APPDATA_DB, "pg_restore", "-U", "postgres", "-d", role,
                            "--exit-on-error"], stdin=f, capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail(f"pg_restore failed: {r.stderr.strip()[-800:]}")


# --- commands --------------------------------------------------------------
def cmd_views(_: argparse.Namespace) -> None:
    out({"views": api_views()})


def cmd_provision(a: argparse.Namespace) -> None:
    """Create or update everything an app needs, then write its runtime env file."""
    app = a.app
    views = [v for v in a.views.split(",") if v]
    declared = [s for s in a.secrets.split(",") if s]
    sdir = app_secrets_dir(app)
    user_secrets = read_kv(sdir / "secrets.env")
    missing = [s for s in declared if s not in user_secrets]
    if missing:
        raise Fail("secrets declared in annex.yaml but not set: " + ", ".join(missing)
                   + f". A human sets them with: annex secret set {app} {missing[0]}  (it asks for the value; or --from-file PATH)")

    env: dict[str, str] = {}
    if a.postgres:
        env["DATABASE_URL"] = ensure_database(app)
    token = grant_views(app, views)
    if token:
        env["ANNEX_GATEWAY_TOKEN"] = token
    ops_token = grant_notify(app, a.notify_email, [c for c in a.slack.split(",") if c])
    if ops_token:
        env["ANNEX_OPS_TOKEN"] = ops_token
    try:
        hooks = json.loads(a.hooks) if a.hooks else []
    except ValueError:
        raise Fail("--hooks must be a JSON list of {name, verify, secret, path}") from None
    hook_token = write_hooks(app, hooks, a.hook_port, user_secrets)
    if hook_token:
        env["ANNEX_HOOK_TOKEN"] = hook_token
    for s in declared:
        env[s] = user_secrets[s]
    if a.files:
        data = DATA / "apps" / app / "data"
        data.mkdir(parents=True, exist_ok=True)
        if data.stat().st_uid != APP_UID:
            as_root(["chown", "-R", f"{APP_UID}:{APP_UID}", str(data)], data)
    write_kv(sdir / "runtime.env", env)
    (DATA / "deploy" / app).mkdir(parents=True, exist_ok=True)
    out({"ok": True, "env_file": str(sdir / "runtime.env"),
         "injected": sorted(env), "views": views})


def cmd_secret_set(a: argparse.Namespace) -> None:
    """Read KEY=VALUE lines on stdin and merge them into the app's secrets.env."""
    path = app_secrets_dir(a.app) / "secrets.env"
    current = read_kv(path)
    keys = []
    for line in sys.stdin.read().splitlines():
        if not line:
            continue
        k, sep, v = line.partition("=")
        if not sep or not KEY_RE.match(k):
            raise Fail(f"bad secret line for key {k!r}: use KEY=VALUE with KEY in A-Z, 0-9, _")
        if "'" in v or "\n" in v:
            raise Fail(f"{k}: values can't contain single quotes or newlines in v0")
        current[k] = v
        keys.append(k)
    write_kv(path, current)
    audit("secret.set", a.actor, a.app, {"names": keys})  # names only, never values
    out({"ok": True, "set": keys, "all_keys": sorted(current)})


def cmd_secret_keys(a: argparse.Namespace) -> None:
    out({"keys": sorted(read_kv(SECRETS / "apps" / a.app / "secrets.env"))})


def cmd_deprovision(a: argparse.Namespace) -> None:
    app = a.app
    drop_product_role(app)
    removed = ["gateway role"]
    if (SECRETS / "hooks" / f"{app}.json").exists():
        write_hooks(app, [], 0, {})
        removed.append("webhook file")
    if ops_db_exists():
        psql(APPDATA_DB, OPS_DB, f"DELETE FROM annex_ops.app_grants WHERE app = '{app}';")
        removed.append("email/Slack grant")
    if not a.keep_data:
        if ops_db_exists():  # a redeployed name starts at r1 again: its old error groups would be wrong
            psql(APPDATA_DB, OPS_DB, f"DELETE FROM annex_ops.error_events WHERE app = '{app}'; "
                                     f"DELETE FROM annex_ops.error_groups WHERE app = '{app}'; "
                                     f"DELETE FROM annex_ops.error_alerts WHERE app = '{app}';")
            removed.append("error groups")
        drop_database(app)
        appdir = DATA / "apps" / app
        if appdir.exists():
            as_root(["rm", "-rf", str(appdir)], appdir.parent)
        shutil.rmtree(SECRETS / "apps" / app, ignore_errors=True)
        shutil.rmtree(SNAPSHOTS / app, ignore_errors=True)
        removed += ["database", "files", "release history", "snapshots", "secrets"]
    shutil.rmtree(DATA / "deploy" / app, ignore_errors=True)
    removed.append("deploy files")
    audit("app.removed", a.actor, app, {"kept_data": a.keep_data})
    out({"ok": True, "removed": removed, "kept_data": a.keep_data})


def cmd_backup_now(_: argparse.Namespace) -> None:
    """Run the nightly backup now (dump hook + archive + retention)."""
    before = set(BACKUPS.glob("annex-*.tar.gz"))
    r = subprocess.run(["docker", "exec", BACKUP, "backup"], capture_output=True, text=True)
    new = sorted(set(BACKUPS.glob("annex-*.tar.gz")) - before)
    if r.returncode != 0 or not new:
        raise Fail(f"backup failed: {(r.stderr or r.stdout).strip()[-800:]}")
    out({"ok": True, "archive": new[-1].name, "size_bytes": new[-1].stat().st_size,
         "archives": sorted(p.name for p in BACKUPS.glob("annex-*.tar.gz"))})


def cmd_restore(a: argparse.Namespace) -> None:
    """Restore one app's database and /data from a backup archive (default: newest)."""
    app = a.app
    record_path = DATA / "deploy" / app / "deploy.json"
    if not record_path.exists():
        raise Fail(f"{app} is not deployed. Deploy it first (annex deploy), then restore its data")
    record = json.loads(record_path.read_text())
    archives = sorted(BACKUPS.glob("annex-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if a.archive:
        archive = BACKUPS / a.archive
        if archive not in archives:
            raise Fail(f"no archive {a.archive}; available: {', '.join(p.name for p in archives) or 'none'}")
    elif archives:
        archive = archives[-1]
    else:
        raise Fail("no backups yet; run scripts/backup.sh first")

    role = role_name(app)
    files_prefix = f"backup/apps/{app}/data"
    dump_name = f"backup/dumps/current/{role}.dump"
    staging = DATA / "restore" / app
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    n_files = 0
    have_dump = False
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            name = m.name.lstrip("/")
            if name == dump_name or name == files_prefix or name.startswith(files_prefix + "/"):
                m.name = name
                tar.extract(m, staging, filter="data")
                n_files += m.isfile() and name != dump_name
                have_dump |= name == dump_name
    if record.get("postgres") and not have_dump:
        raise Fail(f"{archive.name} has no database dump for {app}")

    maintenance(app, 15)
    subprocess.run(["docker", "stop", f"annex-app-{app}"], capture_output=True)
    try:
        if have_dump:
            restore_database(app, staging / dump_name)
        if (staging / files_prefix).exists():
            appdir = DATA / "apps" / app
            appdir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh",
                            "-v", f"{appdir}:/app", "-v", f"{staging}:/staging:ro", helper_image(), "-c",
                            f"rm -rf /app/data && cp -a /staging/{files_prefix} /app/data && "
                            f"chown -R {APP_UID}:{APP_UID} /app/data"], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "start", f"annex-app-{app}"], capture_output=True)
        shutil.rmtree(staging, ignore_errors=True)
        maintenance(app, None)
    audit("data.restored", a.actor, app, {"archive": archive.name, "database": have_dump, "files": n_files})
    out({"ok": True, "app": app, "archive": archive.name, "database": have_dump, "files": n_files})


# --- pre-deploy snapshots (rollback --with-data) -----------------------------
def in_helper(args: list[str], mounts: list[str], stdout=None) -> subprocess.CompletedProcess:
    """Run a command as root in the helper image, with no network."""
    vols = [x for m in mounts for x in ("-v", m)]
    return subprocess.run(["docker", "run", "--rm", "--network", "none", *vols, "--entrypoint", args[0],
                           helper_image(), *args[1:]], stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE)


def snapshot_dirs(app: str) -> list[Path]:
    """The app's snapshots, oldest first."""
    found = [p for p in (SNAPSHOTS / app).glob("pre-r*") if (p / "meta.json").exists() and p.name[5:].isdigit()]
    return sorted(found, key=lambda p: int(p.name[5:]))


def cmd_snapshot(a: argparse.Namespace) -> None:
    """Before release N goes live: dump the app's database, and archive /data if it's under 200 MB."""
    app = a.app
    target = SNAPSHOTS / app / f"pre-r{a.release}"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(mode=0o700, parents=True)
    meta: dict = {"name": target.name, "release": a.release, "from_release": a.from_release,
                  "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "db": False, "files": False}
    try:
        if database_exists(app):
            with open(target / "db.dump", "wb") as f:
                r = subprocess.run(["docker", "exec", APPDATA_DB, "pg_dump", "-U", "postgres", "-Fc", role_name(app)],
                                   stdout=f, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise Fail(f"pg_dump failed: {r.stderr.decode(errors='replace').strip()[-400:]}")
            meta["db"] = True
        data = DATA / "apps" / app / "data"
        if data.is_dir():
            r = in_helper(["du", "-sk", "/d"], [f"{data}:/d:ro"])
            kb = int(r.stdout.split()[0]) if r.returncode == 0 else 0
            if kb > SNAPSHOT_MAX_FILES_KB:
                meta["files"] = f"skipped: /data is {kb // 1024} MB, over 200 MB; the nightly backup covers it"
            else:
                with open(target / "data.tar.gz", "wb") as f:
                    r = in_helper(["tar", "-czf", "-", "-C", "/d", "."], [f"{data}:/d:ro"], stdout=f)
                if r.returncode != 0:
                    raise Fail(f"archiving /data failed: {r.stderr.decode(errors='replace').strip()[-400:]}")
                meta["files"] = True
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    if not meta["db"] and meta["files"] is False:
        shutil.rmtree(target, ignore_errors=True)
        out({"ok": True, "snapshot": None})
        return
    meta["bytes"] = sum(p.stat().st_size for p in target.iterdir())
    (target / "meta.json").write_text(json.dumps(meta))
    out({"ok": True, "snapshot": meta})


def cmd_snapshots(a: argparse.Namespace) -> None:
    out({"ok": True, "snapshots": [json.loads((p / "meta.json").read_text()) for p in snapshot_dirs(a.app)]})


def cmd_restore_snapshot(a: argparse.Namespace) -> None:
    """Replace the app's database and /data with the newest snapshot taken while release K was live.
    Leaves the app's container stopped: the caller starts the release it rolls back to."""
    app = a.app
    snaps = [p for p in snapshot_dirs(app) if json.loads((p / "meta.json").read_text())["from_release"] == a.from_release]
    if not snaps:
        raise Fail(f"no data snapshot from while r{a.from_release} was live; snapshots are taken before each "
                   "deploy and the last 3 are kept. Roll back without --with-data, or restore a nightly backup "
                   f"with scripts/restore.sh {app}")
    snap = snaps[-1]
    meta = json.loads((snap / "meta.json").read_text())
    maintenance(app, 15)  # the CLI ends it once the release it rolls back to is running
    subprocess.run(["docker", "stop", f"annex-app-{app}"], capture_output=True)
    if (snap / "db.dump").exists():
        restore_database(app, snap / "db.dump")
    if (snap / "data.tar.gz").exists():
        appdir = DATA / "apps" / app
        appdir.mkdir(parents=True, exist_ok=True)
        r = in_helper(["sh", "-c", f"rm -rf /app/data && mkdir /app/data && tar -xzf /snap/data.tar.gz -C /app/data "
                                   f"&& chown -R {APP_UID}:{APP_UID} /app/data"],
                      [f"{appdir}:/app", f"{snap}:/snap:ro"])
        if r.returncode != 0:
            raise Fail(f"restoring /data failed: {r.stderr.decode(errors='replace').strip()[-400:]}")
    out({"ok": True, "app": app, "snapshot": meta["name"], "database": (snap / "db.dump").exists(),
         "files": (snap / "data.tar.gz").exists()})


def cmd_prune_snapshots(a: argparse.Namespace) -> None:
    snaps = snapshot_dirs(a.app)
    old = snaps[:-a.keep] if a.keep else snaps
    for p in old:
        shutil.rmtree(p, ignore_errors=True)
    out({"ok": True, "removed": [p.name for p in old], "kept": [p.name for p in snaps[len(old):]]})

# --- releases in the database (M2.2) -----------------------------------------------------------------------------
# releases.jsonl stays the file of record; annex_ops.releases is the copy the console and the audit trail read. The
# CLI calls release-record wherever it appends a line; release-backfill (push.sh) loads every file, idempotently.
# /srv/annex/apps is read here, on the VM: it is never mounted into a container (it holds the apps' data).
RELEASE_COLUMNS = ("app text, release int, image text, result text, deployed_at timestamptz, deployed_by text, via text, "
                   "git jsonb, note text, rollback_of int, manifest jsonb")


def release_row(app: str, rel: dict) -> dict | None:
    """The annex_ops.releases row for one releases.jsonl line (None if the line lacks what a release needs)."""
    if not all(rel.get(k) is not None for k in ("release", "image", "result", "deployed_at")):
        return None
    note = rel.get("error") or ""
    if rel.get("rolled_back_to") is not None:
        note = f"rolled back to r{rel['rolled_back_to']}: {note}"
    rb = rel.get("rollback")
    if rb:
        note = f"rollback from r{rb['from']} to r{rb['to']}" + (" with data" if rb.get("with_data") else "")
    return {"app": app, "release": rel["release"], "image": rel["image"], "result": rel["result"],
            "deployed_at": rel["deployed_at"], "deployed_by": rel.get("deployed_by"), "via": rel.get("via"),
            "git": rel.get("git"), "note": note or None, "rollback_of": rb["from"] if rb else None,
            "manifest": rel.get("manifest") or {}}


def dollar_quote(text: str) -> str:
    """A SQL string literal that needs no escaping: dollar quotes with a tag the text doesn't contain."""
    tag, i = "annexjson", 0
    while f"${tag}$" in text:
        i += 1
        tag = f"annexjson{i}"
    return f"${tag}${text}${tag}$"


def releases_sql(rows: list[dict]) -> str:
    return ("INSERT INTO annex_ops.releases (app, release, image, result, deployed_at, deployed_by, via, git, note, "
            "rollback_of, manifest) SELECT r.app, r.release, r.image, r.result, r.deployed_at, r.deployed_by, r.via, "
            "r.git, r.note, r.rollback_of, r.manifest FROM jsonb_to_recordset("
            f"{dollar_quote(json.dumps(rows))}::jsonb) AS r({RELEASE_COLUMNS}) ON CONFLICT (app, deployed_at, release) DO NOTHING;")


def cmd_release_record(a: argparse.Namespace) -> None:
    """One releases.jsonl line on stdin."""
    rel = json.loads(sys.stdin.read())
    row = release_row(a.app, rel)
    if row is None:
        raise Fail("release-record: the line has no release, image, result or deployed_at")
    if not ops_db_exists():
        raise Fail("annex-ops isn't set up on this server: run scripts/bootstrap.sh and scripts/push.sh")
    psql(APPDATA_DB, OPS_DB, releases_sql([row]) + "\n" + audit_sql([release_audit(a.app, rel)]))  # one call: the release and its audit row
    out({"ok": True, "recorded": 1})


def cmd_release_backfill(_: argparse.Namespace) -> None:
    rows, events = [], []
    for f in sorted((DATA / "apps").glob("*/releases.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                rel = json.loads(line)
                row = release_row(f.parent.name, rel)
                if row:
                    rows.append(row)
                    events.append(release_audit(f.parent.name, rel))
    if rows:
        if not ops_db_exists():
            raise Fail("annex-ops isn't set up on this server: run scripts/bootstrap.sh and scripts/push.sh")
        psql(APPDATA_DB, OPS_DB, releases_sql(rows) + "\n" + audit_sql(events))
    out({"ok": True, "found": len(rows)})


# --- the audit trail (M2.5) ----------------------------------------------------------------------------------------
# annex_ops.audit_events: who did what. Written here for everything this script does, best effort (an unreachable audit
# table must not stop a deploy or an admin's change: it is said on stderr), and by annex-ops for the rest.
def audit_row(action: str, actor: str | None, app: str | None = None, details: dict | None = None, ts: str | None = None,
              ref: str | None = None) -> dict:
    return {"ts": ts, "actor": actor, "app": app, "action": action, "details": details or {}, "ref": ref}


def release_audit(app: str, rel: dict) -> dict | None:
    """The audit row of one releases.jsonl line (pure): deploy, deploy.failed, rollback or rollback.failed."""
    row = release_row(app, rel)
    if row is None:
        return None
    rb = rel.get("rollback")
    failed = rel["result"] == "failed"
    action = ("rollback" if rb else "deploy") + (".failed" if failed else "")
    git = (rel.get("git") or {}).get("sha")
    details = {"release": rel["release"], "via": rel.get("via"), "git": git}
    if rb:
        details.update({"from": rb["from"], "to": rb["to"], "with_data": bool(rb.get("with_data"))})
    if failed:
        details["error"] = (rel.get("error") or "")[:300]
        if rel.get("rolled_back_to") is not None:
            details["rolled_back_to"] = rel["rolled_back_to"]
    return audit_row(action, rel.get("deployed_by"), app, {k: v for k, v in details.items() if v is not None}, rel["deployed_at"],
                     f"release:{app}:{rel['deployed_at']}:{rel['release']}")


def audit_sql(rows: list[dict]) -> str:
    return ("INSERT INTO annex_ops.audit_events (ts, actor, app, action, details, ref) SELECT coalesce(r.ts, now()), r.actor, r.app, "
            "r.action, r.details, r.ref FROM jsonb_to_recordset(" + dollar_quote(json.dumps(rows)) +
            "::jsonb) AS r(ts timestamptz, actor text, app text, action text, details jsonb, ref text) ON CONFLICT (ref) DO NOTHING;")


def audit(action: str, actor: str | None, app: str | None = None, details: dict | None = None) -> None:
    """Record one audit event; never raises (the operation it describes has already happened)."""
    try:
        if ops_db_exists():
            psql(APPDATA_DB, OPS_DB, audit_sql([audit_row(action, actor or "unknown", app, details)]))
    except Exception as e:  # noqa: BLE001
        print(f"annex_vm: couldn't write the audit event {action}: {e}", file=sys.stderr)


# --- Annex's own user accounts (Authelia's file backend, D57) ----------------------------------------------------
# Source of truth: secrets/authelia/users.yml, which Authelia also rewrites when a user resets or changes their
# password. The user directory (state/directory/users.json: no hashes) is derived from it for annex-ops.
USERS_YML = SECRETS / "authelia" / "users.yml"
DIRECTORY = DATA / "state" / "directory" / "users.json"
AUTHELIA = "annex-authelia-1"
LOGIN_RE = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[A-Za-z]{2,}$")


def _yaml():
    try:
        import yaml  # Ubuntu's python3-yaml (a cloud-init dependency, so on every cloud image)
    except ImportError:
        raise Fail("managing users needs PyYAML on the server (Ubuntu package python3-yaml). "
                   "Ask the platform team to install it") from None
    return yaml


def validate_user(login: str | None = None, name: str | None = None, email: str | None = None,
                  groups: list[str] | None = None) -> list[str]:
    """Problems with the given fields, each saying how to fix it (pure; unit-tested)."""
    p = []
    if login is not None and not LOGIN_RE.match(login):
        p.append(f"login {login!r}: use 2-32 lowercase letters, digits, '.', '_' or '-', starting with a letter")
    if name is not None and (not name.strip() or len(name) > 80 or any(c in name for c in "\n\r\"")):
        p.append("name: give a display name of up to 80 characters, e.g. --name 'Ana Kovač'")
    if email is not None and not EMAIL_RE.match(email):
        p.append(f"email {email!r} is not an address, e.g. --email ana@company.com")
    if groups is not None:
        bad = [g for g in groups if not GROUP_RE.match(g)]
        if bad:
            p.append(f"groups {', '.join(bad)}: use lowercase names, e.g. --groups users or --groups admins,builders")
        if not groups:
            p.append("groups: give at least one, e.g. --groups users (admins, builders and users are the usual ones)")
    return p


def directory_entries(users: dict) -> dict:
    """What annex-ops may know about users: no hashes, no disabled accounts."""
    return {login: {"name": u.get("displayname") or login, "email": u.get("email", ""),
                    "groups": list(u.get("groups") or [])}
            for login, u in sorted(users.items()) if not u.get("disabled")}


def active_admins(users: dict) -> list[str]:
    return sorted(login for login, u in users.items() if "admins" in (u.get("groups") or []) and not u.get("disabled"))


def public_user(login: str, u: dict) -> dict:
    return {"login": login, "name": u.get("displayname", ""), "email": u.get("email", ""),
            "groups": list(u.get("groups") or []), "disabled": bool(u.get("disabled"))}


def load_users() -> dict:
    return (_yaml().safe_load(USERS_YML.read_text()) or {}).get("users") or {}


def save_users(users: dict) -> None:
    if not active_admins(users):
        raise Fail("that would leave no active admin. Make another user an admin first, "
                   "e.g. annex users set <login> --groups admins,builders")
    text = _yaml().safe_dump({"users": users}, sort_keys=False, default_flow_style=False, allow_unicode=True,
                             width=1000)
    # atomically (a new file renamed over the old one): Authelia's watcher reloads on every change, and an in-place
    # rewrite let it read a half-written file and keep the old accounts (found by scripts/test-users.sh)
    _replace_users_file(text)
    # Authelia skips a change that comes within about a second of its previous reload, and doesn't catch up later
    # (two quick `annex users` calls left a disabled user able to sign in). Writing the same content again after
    # a pause makes sure the latest state gets its own reload; a reload costs milliseconds.
    time.sleep(1.5)
    _replace_users_file(text)
    write_directory(users)


def _replace_users_file(text: str) -> None:
    tmp = USERS_YML.with_name(".users.yml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(USERS_YML)


def write_directory(users: dict) -> None:
    tmp = DIRECTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(directory_entries(users), indent=1) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(DIRECTORY)  # the directory is mounted, not the file (D7), so a rename is seen


class users_lock:
    def __enter__(self):
        self.f = open(SECRETS / "authelia" / ".users.lock", "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def random_password() -> tuple[str, str]:
    """(password, argon2id digest) from Authelia's own tool, so the hash matches its settings."""
    image = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", AUTHELIA], capture_output=True, text=True)
    if image.returncode != 0:
        raise Fail("Authelia isn't running; start the platform with scripts/push.sh")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", image.stdout.strip(), "authelia", "crypto",
                        "hash", "generate", "argon2", "--random", "--random.length", "24"], capture_output=True, text=True)
    pw = re.search(r"^Random Password: (\S+)$", r.stdout, re.M)
    digest = re.search(r"^Digest: (\S+)$", r.stdout, re.M)
    if r.returncode != 0 or not pw or not digest:
        raise Fail("couldn't generate a password with Authelia's tool: " + (r.stderr or r.stdout).strip()[-300:])
    return pw.group(1), digest.group(1)


def platform_setting(name: str) -> str:
    return read_kv(DATA / "platform" / ".env").get(name, "")


def send_reset_email(login: str) -> None:
    """Ask Authelia to email the user its reset link (also used as the invitation). Authelia only accepts the
    request on its exact public URL (port included), so it goes through Traefik with that Host."""
    auth = platform_setting("ANNEX_URL_AUTH")  # e.g. https://auth.annex.home.arpa:8443
    m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?$", auth)
    if not m:
        raise Fail(f"ANNEX_URL_AUTH is {auth!r}; run scripts/push.sh")
    scheme, host, port = m.group(1), m.group(2), m.group(3) or ("443" if m.group(1) == "https" else "80")
    target = "127.0.0.1:443" if platform_setting("ANNEX_TLS_MODE") == "local" else "127.0.0.1:80"
    r = subprocess.run(["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20",
                        "--connect-to", f"{host}:{port}:{target}", "-H", "Content-Type: application/json",
                        "-d", json.dumps({"username": login}), f"{auth}/api/reset-password/identity/start"],
                       capture_output=True, text=True)
    if r.stdout.strip() == "429":
        raise Fail("Authelia limits how many reset and invitation emails go out (30 per 10 minutes from the "
                   "server). Try again in a few minutes, or use --print-password")
    if r.stdout.strip() != "200":
        raise Fail(f"Authelia didn't accept the reset request (HTTP {r.stdout.strip() or r.stderr.strip()}). "
                   "Use --print-password instead, and check: docker logs annex-authelia-1")


def update_demo_password(login: str, password: str) -> bool:
    """users.txt holds the demo users' passwords (a demo convenience, D12): keep it right when one is reset."""
    path = SECRETS / "users.txt"
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    hit = [i for i, line in enumerate(lines) if line.split(" ", 1)[0] == login]
    for i in hit:
        lines[i] = f"{login} {password}"
    if hit:
        path.write_text("\n".join(lines) + "\n")
    return bool(hit)


def email_outside_domains(email: str) -> bool:
    domains = [d.strip().lower() for d in platform_setting("ANNEX_EMAIL_DOMAINS").split(",") if d.strip()]
    allow = [a.strip().lower() for a in platform_setting("ANNEX_EMAIL_ALLOW").split(",") if a.strip()]
    return bool(domains) and email.lower().split("@")[-1] not in domains and email.lower() not in allow


def cmd_users_list(_: argparse.Namespace) -> None:
    out({"ok": True, "users": [public_user(login, u) for login, u in sorted(load_users().items())]})


def _groups(text: str | None) -> list[str] | None:
    return None if text is None else [g.strip() for g in text.split(",") if g.strip()]


def cmd_users_add(a: argparse.Namespace) -> None:
    groups = _groups(a.groups)
    problems = validate_user(a.login, a.name, a.email, groups)
    if problems:
        raise Fail("; ".join(problems))
    with users_lock():
        users = load_users()
        if a.login in users:
            raise Fail(f"{a.login} already exists. Change it with: annex users set {a.login} ...")
        if any((u.get("email") or "").lower() == a.email.lower() for u in users.values()):
            raise Fail(f"another user already has {a.email}; each account needs its own address")
        password, digest = random_password()
        users[a.login] = {"disabled": False, "displayname": a.name, "email": a.email, "password": digest,
                          "groups": groups}
        save_users(users)
    audit("user.added", a.actor, None, {"login": a.login, "groups": groups, "invited": not a.print_password})
    res = {"ok": True, "user": public_user(a.login, users[a.login])}
    if a.print_password:
        res["password"] = password  # human-only, printed once by the CLI (the only secret this script prints)
    else:
        send_reset_email(a.login)
        res["invited"] = a.email
    if email_outside_domains(a.email):
        res["warning"] = (f"{a.email} isn't in ANNEX_EMAIL_DOMAINS, so Annex's own emails (alerts, digests, app "
                          "emails) won't reach it; the invitation does, because Authelia sends that itself")
    out(res)


def cmd_users_set(a: argparse.Namespace) -> None:
    groups = _groups(a.groups)
    problems = validate_user(None, a.name, a.email, groups)
    if problems:
        raise Fail("; ".join(problems))
    with users_lock():
        users = load_users()
        if a.login not in users:
            raise Fail(f"no user {a.login!r}. See: annex users list")
        u = users[a.login]
        if a.name is not None:
            u["displayname"] = a.name
        if a.email is not None:
            u["email"] = a.email
        if groups is not None:
            u["groups"] = groups
        if a.disable:
            u["disabled"] = True
        if a.enable:
            u["disabled"] = False
        save_users(users)
    audit("user.changed", a.actor, None, {"login": a.login, "set": [k for k in ("name", "email", "groups") if getattr(a, k) is not None]
                                          + (["disabled"] if a.disable else []) + (["enabled"] if a.enable else []),
                                          **({"groups": groups} if groups is not None else {})})
    out({"ok": True, "user": public_user(a.login, users[a.login])})


def cmd_users_remove(a: argparse.Namespace) -> None:
    with users_lock():
        users = load_users()
        if a.login not in users:
            raise Fail(f"no user {a.login!r}. See: annex users list")
        del users[a.login]
        save_users(users)
    audit("user.removed", a.actor, None, {"login": a.login})
    out({"ok": True, "removed": a.login,
         "note": "apps they own keep running; give them a new owner in annex.yaml and deploy again"})


def cmd_users_reset(a: argparse.Namespace) -> None:
    with users_lock():
        users = load_users()
        u = users.get(a.login)
        if u is None:
            raise Fail(f"no user {a.login!r}. See: annex users list")
        if u.get("disabled"):
            raise Fail(f"{a.login} is disabled. Enable it first: annex users set {a.login} --enable")
        if a.print_password:
            password, u["password"] = random_password()
            save_users(users)
            demo = update_demo_password(a.login, password)
            audit("user.reset", a.actor, None, {"login": a.login, "how": "password printed once"})
            out({"ok": True, "login": a.login, "password": password, "demo_password_updated": demo})
            return
    send_reset_email(a.login)
    audit("user.reset", a.actor, None, {"login": a.login, "how": "reset link emailed"})
    out({"ok": True, "login": a.login, "reset_sent_to": u.get("email", "")})


def cmd_users_sync(_: argparse.Namespace) -> None:
    users = load_users()
    write_directory(users)
    out({"ok": True, "directory": len(directory_entries(users))})


def main() -> None:
    p = argparse.ArgumentParser(prog="annex_vm")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("views").set_defaults(fn=cmd_views)
    s = sub.add_parser("provision")
    s.add_argument("app")
    s.add_argument("--postgres", action="store_true")
    s.add_argument("--files", action="store_true")
    s.add_argument("--views", default="")
    s.add_argument("--secrets", default="")
    s.add_argument("--notify-email", action="store_true", help="grant email through annex-ops")
    s.add_argument("--slack", default="", help="comma-separated Slack channels the app may post to, e.g. '#a,#b'")
    s.add_argument("--hooks", default="", help="JSON list of incoming webhooks: [{name, verify, secret, path}]")
    s.add_argument("--hook-port", type=int, default=0, help="the app's port, for forwarding webhooks")
    s.set_defaults(fn=cmd_provision)
    s = sub.add_parser("secret-set")
    s.add_argument("app")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.set_defaults(fn=cmd_secret_set)
    s = sub.add_parser("secret-keys")
    s.add_argument("app")
    s.set_defaults(fn=cmd_secret_keys)
    s = sub.add_parser("deprovision")
    s.add_argument("app")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.add_argument("--keep-data", action="store_true")
    s.set_defaults(fn=cmd_deprovision)
    sub.add_parser("backup-now").set_defaults(fn=cmd_backup_now)
    s = sub.add_parser("restore")
    s.add_argument("app")
    s.add_argument("--actor", default=os.environ.get("ANNEX_ACTOR", "unknown"), help="who is doing this, for the audit trail")
    s.add_argument("--archive")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("snapshot")
    s.add_argument("app")
    s.add_argument("--release", type=int, required=True)
    s.add_argument("--from-release", type=int, required=True)
    s.set_defaults(fn=cmd_snapshot)
    s = sub.add_parser("snapshots")
    s.add_argument("app")
    s.set_defaults(fn=cmd_snapshots)
    s = sub.add_parser("restore-snapshot")
    s.add_argument("app")
    s.add_argument("--from-release", type=int, required=True)
    s.set_defaults(fn=cmd_restore_snapshot)
    s = sub.add_parser("prune-snapshots")
    s.add_argument("app")
    s.add_argument("--keep", type=int, default=3)
    s.set_defaults(fn=cmd_prune_snapshots)
    s = sub.add_parser("release-record", help="store one releases.jsonl line (on stdin) in annex_ops.releases")
    s.add_argument("app")
    s.set_defaults(fn=cmd_release_record)
    sub.add_parser("release-backfill", help="load every app's releases.jsonl into annex_ops.releases (idempotent)").set_defaults(
        fn=cmd_release_backfill)
    sub.add_parser("users-list").set_defaults(fn=cmd_users_list)
    sub.add_parser("users-sync").set_defaults(fn=cmd_users_sync)
    s = sub.add_parser("users-add")
    s.add_argument("login")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.add_argument("--name", required=True)
    s.add_argument("--email", required=True)
    s.add_argument("--groups", default="users")
    s.add_argument("--print-password", action="store_true")
    s.set_defaults(fn=cmd_users_add)
    s = sub.add_parser("users-set")
    s.add_argument("login")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.add_argument("--name")
    s.add_argument("--email")
    s.add_argument("--groups")
    s.add_argument("--disable", action="store_true")
    s.add_argument("--enable", action="store_true")
    s.set_defaults(fn=cmd_users_set)
    s = sub.add_parser("users-remove")
    s.add_argument("login")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.set_defaults(fn=cmd_users_remove)
    s = sub.add_parser("users-reset")
    s.add_argument("login")
    s.add_argument("--actor", default="unknown", help="who is doing this, for the audit trail")
    s.add_argument("--print-password", action="store_true")
    s.set_defaults(fn=cmd_users_reset)
    a = p.parse_args()
    if hasattr(a, "app") and not NAME_RE.match(a.app):
        out({"ok": False, "error": f"invalid app name {a.app!r}"})
        sys.exit(1)
    try:
        a.fn(a)
    except Fail as e:
        out({"ok": False, "error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
