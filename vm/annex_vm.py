#!/usr/bin/env python3
"""VM-side app operations for the annex CLI.

Runs ON THE VM with the stock python3 (stdlib only), called over SSH by the
CLI. Database passwords and gateway tokens are generated and kept here, in
/srv/annex/secrets/apps/<app>/. Every command prints one JSON object; secret
values are never printed.
"""
from __future__ import annotations

import argparse
import base64
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
APP_UID = 10001
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


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


def jwt(role: str, app: str) -> str:
    key = read_kv(SECRETS / "master.env")["GATEWAY_JWT_SECRET"]

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps({"role": role, "app": app, "iat": int(time.time())}, separators=(",", ":")).encode())
    sig = b64(hmac.new(key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def api_views() -> list[str]:
    rows = psql(PRODUCT_DB, "product",
                "SELECT table_name FROM information_schema.views WHERE table_schema = 'annex_api' ORDER BY 1;")
    return [r for r in rows.splitlines() if r]


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
    token_path = app_secrets_dir(app) / "gateway_token"
    if not token_path.exists():
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(jwt(role, app))
    return token_path.read_text().strip()


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
                   + f". A human sets them with: annex secret set {app} {missing[0]}=<value>")

    env: dict[str, str] = {}
    if a.postgres:
        env["DATABASE_URL"] = ensure_database(app)
    token = grant_views(app, views)
    if token:
        env["ANNEX_GATEWAY_TOKEN"] = token
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
    out({"ok": True, "set": keys, "all_keys": sorted(current)})


def cmd_secret_keys(a: argparse.Namespace) -> None:
    out({"keys": sorted(read_kv(SECRETS / "apps" / a.app / "secrets.env"))})


def cmd_deprovision(a: argparse.Namespace) -> None:
    app = a.app
    drop_product_role(app)
    removed = ["gateway role"]
    if not a.keep_data:
        drop_database(app)
        appdir = DATA / "apps" / app
        if appdir.exists():
            as_root(["rm", "-rf", str(appdir)], appdir.parent)
        shutil.rmtree(SECRETS / "apps" / app, ignore_errors=True)
        removed += ["database", "files", "secrets"]
    shutil.rmtree(DATA / "deploy" / app, ignore_errors=True)
    removed.append("deploy files")
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

    subprocess.run(["docker", "stop", f"annex-app-{app}"], capture_output=True)
    try:
        if have_dump:
            psql(APPDATA_DB, "postgres", f"DROP DATABASE IF EXISTS {role} WITH (FORCE);")
            ensure_database(app)
            with open(staging / dump_name, "rb") as f:
                r = subprocess.run(["docker", "exec", "-i", APPDATA_DB, "pg_restore", "-U", "postgres", "-d", role,
                                    "--exit-on-error"], stdin=f, capture_output=True, text=True)
            if r.returncode != 0:
                raise Fail(f"pg_restore failed: {r.stderr.strip()[-800:]}")
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
    out({"ok": True, "app": app, "archive": archive.name, "database": have_dump, "files": n_files})


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
    s.set_defaults(fn=cmd_provision)
    s = sub.add_parser("secret-set")
    s.add_argument("app")
    s.set_defaults(fn=cmd_secret_set)
    s = sub.add_parser("secret-keys")
    s.add_argument("app")
    s.set_defaults(fn=cmd_secret_keys)
    s = sub.add_parser("deprovision")
    s.add_argument("app")
    s.add_argument("--keep-data", action="store_true")
    s.set_defaults(fn=cmd_deprovision)
    sub.add_parser("backup-now").set_defaults(fn=cmd_backup_now)
    s = sub.add_parser("restore")
    s.add_argument("app")
    s.add_argument("--archive")
    s.set_defaults(fn=cmd_restore)
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
