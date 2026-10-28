#!/usr/bin/env python3
"""Build a variant of the Annex self-test app (tests/annex-selftest) for a test to deploy. Standard library only.

  tests/selftest.py build OUT [options]

The fixture's annex.yaml is regular on purpose: optional parts sit between `# --- name ---` and `# --- end ---`
lines, everything else is one `key: value` line, so a variant is a few text edits:

  --name N           the app's name (default annex-selftest; variants of it are named annex-selftest-<what>)
  --owner LOGIN      owner (default sam)
  --drop a,b         remove sections: web, notify, webhooks (dropping webhooks also empties the secrets list)
  --job-only         type: job: no web part, no webhooks (the app idles; its jobs run through the scheduler)
  --broken-health    the health check path is /nope, which answers 404: the deploy fails its health gate
  --no-postgres      storage.postgres: false
  --evil-label       the Dockerfile adds LABEL traefik.http.routers.evil.rule=...: the deploy must refuse the image (D83)
  --env K=V         set or add an `env:` entry (SELFTEST_LOG=plain, SELFTEST_MARKER=..., SELFTEST_BAD_MIGRATION=1,
                     SELFTEST_SLOW_HEALTH=N: /healthz answers 503 for N s after start, ...)
  --secret NAME      declare one more secret
  --no-views         no gateway views (data.views: [])
  --views a,b        these gateway views instead of the fixture's (e.g. the demo client's: leaderboard,machine_floor)
  --egress a,b       declare these hostnames under egress: (the app gets internet access, D88; default: none)
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "annex-selftest"
SECTION = re.compile(r"^# --- (\w+) ---\n(.*?)^# --- end ---\n", re.S | re.M)


def drop_section(text: str, name: str) -> str:
    new, n = re.subn(rf"^# --- {name} ---\n.*?^# --- end ---\n", "", text, flags=re.S | re.M)
    if not n:
        sys.exit(f"selftest.py: there is no section {name!r} to drop")
    return new


def keep_sections(text: str) -> str:
    """The markers themselves are comments; leave only the content of the sections that stay."""
    return SECTION.sub(lambda m: m.group(2), text)


def set_line(text: str, key: str, value: str) -> str:
    new, n = re.subn(rf"^{key}:.*$", f"{key}: {value}", text, flags=re.M)
    if not n:
        sys.exit(f"selftest.py: annex.yaml has no top-level {key}:")
    return new


def set_env(text: str, key: str, value: str) -> str:
    if re.search(rf"^  {key}:", text, re.M):
        return re.sub(rf"^  {key}:.*$", f"  {key}: {value}", text, flags=re.M)
    return text.replace("\nenv:\n", f"\nenv:\n  {key}: {value}\n", 1)


def build(a: argparse.Namespace) -> None:
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(FIXTURE, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak"))
    y = (out / "annex.yaml").read_text()
    drops = [d for d in (a.drop or "").split(",") if d]
    if a.job_only:
        y = set_line(y, "type", "job")
        drops += [d for d in ("web", "webhooks") if d not in drops]
    for d in drops:
        y = drop_section(y, d)
    y = keep_sections(y)
    secrets = [] if ("webhooks" in drops) else ["TRELLO_SECRET", "GH_SECRET", "PLAIN_SECRET"]
    y = set_line(y, "secrets", "[" + ", ".join(secrets + (a.secret or [])) + "]")
    y = set_line(y, "name", a.name)
    y = set_line(y, "owner", a.owner)
    if a.broken_health:
        y = set_line(y, "healthcheck", "/nope")
    if a.no_postgres:
        y = y.replace("postgres: true", "postgres: false")
    if a.no_views:
        y = re.sub(r"^  views: \[.*?\]", "  views: []", y, flags=re.M)
    if a.views:
        y = re.sub(r"^  views: \[.*?\]", "  views: [" + ", ".join(v for v in a.views.split(",") if v) + "]", y, flags=re.M)
    if a.egress:
        y = set_line(y, "egress", "[" + ", ".join(h for h in a.egress.split(",") if h) + "]")
    for kv in a.env or []:
        k, _, v = kv.partition("=")
        y = set_env(y, k, v)
    (out / "annex.yaml").write_text(y)
    if a.big_file:  # D95: an image bigger than the cap; random data, so no layer compression hides it
        df = out / "Dockerfile"
        df.write_text(df.read_text().replace("USER 10001\n", f"RUN head -c {int(a.big_file) * 1024 * 1024} /dev/urandom > /opt/big.bin\nUSER 10001\n", 1))
    if a.evil_label:
        df = out / "Dockerfile"
        label = "LABEL traefik.http.routers.evil.rule=Host(`evil.example`)\n"
        df.write_text(df.read_text().replace("USER 10001\n", "USER 10001\n" + label, 1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("out")
    b.add_argument("--name", default="annex-selftest")
    b.add_argument("--owner", default="sam")
    b.add_argument("--drop")
    b.add_argument("--job-only", action="store_true")
    b.add_argument("--broken-health", action="store_true")
    b.add_argument("--evil-label", action="store_true")
    b.add_argument("--big-file", type=int, metavar="MB")
    b.add_argument("--no-postgres", action="store_true")
    b.add_argument("--no-views", action="store_true")
    b.add_argument("--views")
    b.add_argument("--egress")
    b.add_argument("--env", action="append")
    b.add_argument("--secret", action="append")
    b.set_defaults(fn=build)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
