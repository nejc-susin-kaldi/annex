#!/usr/bin/env python3
"""Applies db/appdata/migrations/*.sql to the annex_ops database (D93). Runs ON THE VM, stdlib only.

  python3 vm/migrate.py [--dir <migrations folder>] [--list]

Run from the platform folder (where compose.yaml is) by scripts/push.sh after db/appdata/bootstrap.sql (roles and
the database). Files are named NNN_name.sql and applied in order, each in one transaction (psql --single-transaction,
ON_ERROR_STOP) as role annex_ops, together with the row in annex_ops.schema_version, so a file is either applied and
recorded or not applied at all. It stops at the first failure and names the file. No password is needed or passed:
psql connects as postgres inside the appdata-db container. Never edit an applied file: add a new one.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def validate(names):
    """Sorted file names, or ValueError saying what is wrong (a bad name, a number used twice)."""
    seen = {}
    for n in names:
        m = NAME.match(n)
        if not m:
            raise ValueError("migration file name %r must look like 004_what_it_does.sql (three digits, lower case, underscores)" % n)
        if m.group(1) in seen:
            raise ValueError("two migrations share the number %s: %s and %s" % (m.group(1), seen[m.group(1)], n))
        seen[m.group(1)] = n
    return sorted(names)


def pending(files, applied):
    """The files still to apply, in order. `applied` holds versions (the file name without .sql).

    A file that is not applied but sorts before one that is would be applied out of order (or never, if the rule were
    "everything after the newest"), so it is an error: renumber it."""
    files = validate(files)
    done = set(applied)
    versions = [f[:-4] for f in files]
    todo = [f for f, v in zip(files, versions) if v not in done]
    newest = max((v for v in versions if v in done), default=None)
    if newest is not None:
        late = [f for f in todo if f[:-4] < newest]
        if late:
            raise ValueError("%s is not applied but %s is: a migration must be numbered above the newest applied one; "
                             "renumber the file" % (", ".join(late), newest))
    return todo


def psql_cmd(*extra):
    return ["docker", "compose", "exec", "-T", "-e", "PGOPTIONS=-c client_min_messages=warning", "appdata-db",
            "psql", "-q", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "annex_ops", *extra]


# as annex_ops, the schema's owner, so every object is owned the way the services expect
SETUP = ("SET ROLE annex_ops; CREATE SCHEMA IF NOT EXISTS annex_ops; CREATE TABLE IF NOT EXISTS annex_ops.schema_version ("
         "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());")


def applied_versions():
    run = subprocess.run(psql_cmd("-At", "-c", SETUP, "-c", "SELECT version FROM annex_ops.schema_version ORDER BY version"),
                         capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError("could not read annex_ops.schema_version: " + run.stderr.strip())
    # with -q only the SELECT prints rows
    return [x for x in run.stdout.split() if x]


def apply(path: Path) -> None:
    version = path.name[:-4]  # validated: digits, lower-case letters and underscores only
    with path.open("rb") as f:
        run = subprocess.run(
            psql_cmd("--single-transaction", "-c", "SET ROLE annex_ops", "-f", "-",
                     "-c", "INSERT INTO annex_ops.schema_version (version) VALUES ('%s')" % version),
            stdin=f, stderr=subprocess.PIPE, text=True)
    if run.returncode != 0:
        raise RuntimeError("migration %s failed and was rolled back:\n%s" % (path.name, run.stderr.strip()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "db" / "appdata" / "migrations"))
    ap.add_argument("--list", action="store_true", help="show what is pending and apply nothing")
    args = ap.parse_args(argv)
    folder = Path(args.dir)
    try:
        names = [p.name for p in folder.glob("*.sql")]
        if not names:
            raise ValueError("no migrations in %s" % folder)
        todo = pending(names, applied_versions())
        if args.list:
            print("\n".join(todo) if todo else "(none pending)")
            return 0
        for n in todo:
            print("migrate: applying %s" % n, flush=True)
            apply(folder / n)
        print("migrate: %d applied, %d already in place" % (len(todo), len(names) - len(todo)))
        return 0
    except (ValueError, RuntimeError) as e:
        print("migrate: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
