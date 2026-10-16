#!/usr/bin/env python3
"""Render the platform bundle into .build/platform.

Reads .env (falls back to .env.example), derives hostnames and mode-specific
settings, renders every *.tmpl under config/ with {{VAR}} placeholders, and
copies compose files, config, db/, vm/, ops/ and images/ alongside. The resulting .env in the
bundle holds non-secret settings only; it is used by Compose on the VM and read
by the annex CLI as the source of truth for the deployed platform.

Stdlib only, so it runs with any Python 3.9+.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".build" / "platform"
PLATFORM_SERVICES = ["home", "auth", "metabase", "logs", "gateway", "mail", "status", "hooks", "console"]
# Used in templates only; kept out of .env because Compose would try to interpolate "$".
TEMPLATE_ONLY = {"ANNEX_HOST_REGEX"}


def die(msg: str) -> None:
    print(f"render: {msg}", file=sys.stderr)
    sys.exit(1)


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def derive(env: dict[str, str]) -> dict[str, str]:
    pattern = env.get("ANNEX_HOST_PATTERN", "")
    if pattern.count("{name}") != 1:
        die("ANNEX_HOST_PATTERN must contain {name} exactly once, e.g. {name}.annex.home.arpa")
    first, _, rest = pattern.partition(".")
    if "{name}" not in first or not rest or "." not in rest:
        die("{name} must be in the first label and be followed by at least two labels, "
            "e.g. {name}.annex.home.arpa or annex-{name}.example.com")
    cookie = env.get("ANNEX_COOKIE_DOMAIN") or rest
    if not (rest == cookie or rest.endswith("." + cookie)):
        die(f"ANNEX_COOKIE_DOMAIN={cookie} must be {rest} or a parent of it")

    mode = env.get("ANNEX_TLS_MODE", "local")
    if mode not in ("local", "upstream"):
        die("ANNEX_TLS_MODE must be local or upstream")
    upstream_ip = env.get("ANNEX_UPSTREAM_IP", "")
    if mode == "upstream" and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", upstream_ip):
        die("ANNEX_TLS_MODE=upstream needs ANNEX_UPSTREAM_IP set to the upstream proxy's LAN IP")

    port = env.get("ANNEX_PUBLIC_PORT", "")
    if port and not port.isdigit():
        die("ANNEX_PUBLIC_PORT must be a number or empty")
    port_suffix = f":{port}" if port and port != "443" else ""

    prefix, _, suffix = pattern.partition("{name}")
    out = {
        "COMPOSE_PROJECT_NAME": "annex",
        "COMPOSE_FILE": "compose.yaml:compose.tls-local.yaml" if mode == "local" else "compose.yaml",
        "ANNEX_DATA_DIR": env.get("ANNEX_DATA_DIR", "/srv/annex"),
        "ANNEX_HOST_PATTERN": pattern,
        "ANNEX_COOKIE_DOMAIN": cookie,
        "ANNEX_TLS_MODE": mode,
        "ANNEX_UPSTREAM_IP": upstream_ip,
        "ANNEX_PUBLIC_PORT": port,
        "ANNEX_PORT_SUFFIX": port_suffix,
        "ANNEX_ENTRYPOINT": "websecure" if mode == "local" else "web",
        "ANNEX_HOST_REGEX": "^" + re.escape(prefix) + "[a-z0-9-]+" + re.escape(suffix) + "$",
        "ANNEX_CERT_NAMES": f"*.{rest} {rest}",
        "TZ": env.get("TZ", "Europe/Ljubljana"),
        "ANNEX_EMAIL_DOMAINS": env.get("ANNEX_EMAIL_DOMAINS", "annex.example"),
        "ANNEX_EMAIL_ALLOW": env.get("ANNEX_EMAIL_ALLOW", ""),
        "ANNEX_EMAIL_FROM": env.get("ANNEX_EMAIL_FROM", "noreply@annex.example"),
        "ANNEX_ALERT_SLACK_CHANNEL": env.get("ANNEX_ALERT_SLACK_CHANNEL", "#annex-alerts"),
        # what Gatus (inside the VM) probes to check the front door: no port suffix
        "ANNEX_FRONTDOOR_URL": ("https" if mode == "local" else "http") + "://" + pattern.replace("{name}", "home") + "/",
    }
    for svc in PLATFORM_SERVICES:
        host = pattern.replace("{name}", svc)
        out[f"ANNEX_HOST_{svc.upper()}"] = host
        out[f"ANNEX_URL_{svc.upper()}"] = f"https://{host}{port_suffix}"
    return out


def render_text(text: str, values: dict[str, str], src: Path) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            die(f"{src.relative_to(ROOT)}: unknown placeholder {{{{{key}}}}}")
        return values[key]

    return re.sub(r"\{\{\s*([A-Z0-9_]+)\s*\}\}", sub, text)


def main() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        env_file = ROOT / ".env.example"
    values = derive(read_env(env_file))

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name in ("compose.yaml", "compose.tls-local.yaml"):
        shutil.copy2(ROOT / name, OUT / name)
    for d in ("config", "db", "vm"):
        shutil.copytree(ROOT / d, OUT / d)
    # annex-ops is the first platform image built from source on the VM
    shutil.copytree(ROOT / "ops", OUT / "ops",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))
    # derived images (built on the VM from a pinned upstream image)
    shutil.copytree(ROOT / "images", OUT / "images")

    # Mode-specific Traefik files: keep the one for this mode, drop the other.
    tdir = OUT / "config" / "traefik"
    mode = values["ANNEX_TLS_MODE"]
    (tdir / f"traefik-{mode}.yml.tmpl").rename(tdir / "traefik.yml.tmpl")
    for other in tdir.glob("traefik-*.yml.tmpl"):
        other.unlink()
    if mode != "local":
        (tdir / "dynamic" / "tls.yml").unlink()

    for tmpl in OUT.rglob("*.tmpl"):
        tmpl.with_suffix("").write_text(render_text(tmpl.read_text(), values, tmpl))
        tmpl.unlink()

    # A config change must recreate the container that reads it (Compose only
    # compares container config, not bind-mounted file contents).
    for svc in ("traefik", "authelia", "homepage", "gatus"):
        h = hashlib.sha256()
        for f in sorted((OUT / "config" / svc).rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(OUT)).encode() + b"\0" + f.read_bytes())
        values[f"ANNEX_CONFIG_HASH_{svc.upper()}"] = h.hexdigest()[:12]

    (OUT / ".env").write_text(
        "# Generated by scripts/render.py. Non-secret settings only.\n"
        + "".join(f"{k}={v}\n" for k, v in values.items() if k not in TEMPLATE_ONLY)
    )
    print(f"render: {env_file.name} -> {OUT.relative_to(ROOT)} (tls={mode}, home={values['ANNEX_URL_HOME']})")


if __name__ == "__main__":
    main()
