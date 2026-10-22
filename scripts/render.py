#!/usr/bin/env python3
"""Render the platform bundle into .build/<env>/platform.

ANNEX_ENV (an environment variable, per command) picks the settings file: unset or
`vm` reads .env (falls back to .env.example) into .build/vm/platform; `local` reads
.env.local (falls back to .env.local.example) into .build/local/platform, the stack
on this machine's Docker (D99). It derives hostnames and mode-specific
settings, renders every *.tmpl under config/ with {{VAR}} placeholders, and
copies compose files, config, db/, vm/ and ops/ alongside. The resulting .env in the
bundle holds non-secret settings only; it is used by Compose on the VM and read
by the annex CLI as the source of truth for the deployed platform.

Stdlib only, so it runs with any Python 3.9+.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENVS = {"vm": ".env", "local": ".env.local"}  # ANNEX_ENV -> settings file (each falls back to <file>.example)
TARGETS = ("ssh", "local")                     # ANNEX_TARGET: how scripts and the CLI reach the platform's Docker
BIND_RE = re.compile(r"^(?:(\d{1,3}(?:\.\d{1,3}){3}):)?(\d{1,5})$")
PLATFORM_SERVICES = ["home", "auth", "metabase", "logs", "gateway", "mail", "hooks", "console"]
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


def env_name(environ) -> str:
    name = environ.get("ANNEX_ENV") or "vm"
    if name not in ENVS:
        die(f"ANNEX_ENV={name} must be vm (the default: .env) or local (.env.local)")
    return name


def env_file(name: str, root: Path = ROOT) -> Path:
    """The settings file of an environment: .env or .env.local, else its committed .example."""
    path = root / ENVS[name]
    return path if path.exists() else root / (ENVS[name] + ".example")


def out_dir(name: str, root: Path = ROOT) -> Path:
    return root / ".build" / name / "platform"


def expand_path(value: str) -> str:
    """$HOME, ${HOME} and ~ in ANNEX_DATA_DIR, the way bash expands them when lib.sh sources the file."""
    return str(Path(os.path.expandvars(value)).expanduser()) if value else value


def bind(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key) or default
    m = BIND_RE.match(value)
    if not m or not 0 < int(m.group(2)) < 65536:
        die(f"{key}={value} must be a port (443) or an IPv4 address and port (127.0.0.1:9443)")
    return value


def derive(env: dict[str, str], name: str = "vm", root: Path = ROOT) -> dict[str, str]:
    target = env.get("ANNEX_TARGET") or "ssh"
    if target not in TARGETS:
        die(f"ANNEX_TARGET={target} must be ssh (the VM, the default) or local (this machine's Docker)")
    local = target == "local"
    data_dir = expand_path(env.get("ANNEX_DATA_DIR") or "/srv/annex")
    if local:
        if not os.path.isabs(data_dir) or "$" in data_dir:
            die(f"ANNEX_DATA_DIR={data_dir} must be an absolute path for a local stack, e.g. $HOME/.annex/local")
        resolved, top = Path(data_dir).resolve(), root.resolve()
        if resolved == top or top in resolved.parents:
            die(f"ANNEX_DATA_DIR={data_dir} is inside the repo; put it outside (rsync and git must never see it), "
                "e.g. $HOME/.annex/local")

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
    # Where Traefik publishes (compose.yaml, compose.tls-local.yaml): every interface on the VM, as before; loopback
    # ports on a Mac, where 443 is taken and the VM's tunnel already holds 127.0.0.1:8443 (D99)
    bind_http = bind(env, "ANNEX_BIND_HTTP", "127.0.0.1:9080" if local else "80")
    bind_https = bind(env, "ANNEX_BIND_HTTPS", "127.0.0.1:9443" if local else "443")
    if local and mode == "local" and (port or "443") != BIND_RE.match(bind_https).group(2):
        die(f"ANNEX_PUBLIC_PORT={port} must be the port of ANNEX_BIND_HTTPS={bind_https}: a local stack is reached "
            "there directly, and every URL and login redirect carries that port")

    app_subnets = env.get("ANNEX_APP_SUBNETS") or "10.213.0.0/16"
    try:
        subnets = ipaddress.ip_network(app_subnets, strict=True)
    except ValueError:
        die(f"ANNEX_APP_SUBNETS={app_subnets} is not a network like 10.213.0.0/16")
    if subnets.version != 4 or subnets.prefixlen > 24:
        die("ANNEX_APP_SUBNETS must be an IPv4 network of /24 or larger, e.g. 10.213.0.0/16")

    prefix, _, suffix = pattern.partition("{name}")
    out = {
        "COMPOSE_PROJECT_NAME": "annex",
        "COMPOSE_FILE": "compose.yaml:compose.tls-local.yaml" if mode == "local" else "compose.yaml",
        "ANNEX_ENV": name,
        "ANNEX_TARGET": target,
        "ANNEX_DATA_DIR": data_dir,
        "ANNEX_HOST_PATTERN": pattern,
        "ANNEX_COOKIE_DOMAIN": cookie,
        "ANNEX_TLS_MODE": mode,
        "ANNEX_UPSTREAM_IP": upstream_ip,
        "ANNEX_PUBLIC_PORT": port,
        "ANNEX_PORT_SUFFIX": port_suffix,
        "ANNEX_BIND_HTTP": bind_http,
        "ANNEX_BIND_HTTPS": bind_https,
        "ANNEX_ENTRYPOINT": "websecure" if mode == "local" else "web",
        "ANNEX_HOST_REGEX": "^" + re.escape(prefix) + "[a-z0-9-]+" + re.escape(suffix) + "$",
        "ANNEX_CERT_NAMES": f"*.{rest} {rest}",
        "TZ": env.get("TZ", "Europe/Ljubljana"),
        "ANNEX_EMAIL_DOMAINS": env.get("ANNEX_EMAIL_DOMAINS", "annex.example"),
        "ANNEX_EMAIL_ALLOW": env.get("ANNEX_EMAIL_ALLOW", ""),
        "ANNEX_EMAIL_FROM": env.get("ANNEX_EMAIL_FROM", "noreply@annex.example"),
        "ANNEX_ALERT_SLACK_CHANNEL": env.get("ANNEX_ALERT_SLACK_CHANNEL", "#annex-alerts"),
        # per-app networks (D88): read by vm/annex_vm.py networks-sync from the platform .env
        "ANNEX_APP_SUBNETS": app_subnets,
        # build limits (D95), read by the CLI from the platform .env; empty = the CLI's defaults (3 GB, 2048 MB)
        "ANNEX_BUILD_MIN_FREE_GB": env.get("ANNEX_BUILD_MIN_FREE_GB", ""),
        "ANNEX_IMAGE_MAX_MB": env.get("ANNEX_IMAGE_MAX_MB", ""),
        # what annex-ops (inside the VM) requests to check the front door: no port suffix
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


def version_stamp(env_file: Path) -> str:
    """The push stamp (scripts/pushcheck.py compares it with the VM's): commit, dirty flag, hash of .env."""
    def git(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    dirty = "true" if git("status", "--porcelain") else "false"
    return (f"commit={git('rev-parse', 'HEAD')}\ndirty={dirty}\n"
            f"env_sha256={hashlib.sha256(env_file.read_bytes()).hexdigest()}\n")


def main() -> None:
    name = env_name(os.environ)
    settings = env_file(name)
    OUT = out_dir(name)
    values = derive(read_env(settings), name)

    # the render before D99 went to .build/platform; nothing reads it any more, so a stale copy can't mislead anyone
    shutil.rmtree(ROOT / ".build" / "platform", ignore_errors=True)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name in ("compose.yaml", "compose.tls-local.yaml"):
        shutil.copy2(ROOT / name, OUT / name)
    for d in ("config", "db", "vm"):
        shutil.copytree(ROOT / d, OUT / d)
    # the deploy runs on the VM (vm/annex_deploy.py, D101) with the CLI's own manifest and log-convention modules
    for src, dest in (("manifest.py", "annex_manifest.py"), ("logcheck.py", "annex_logcheck.py")):
        shutil.copy2(ROOT / "cli" / "src" / "annex_cli" / src, OUT / "vm" / dest)
    # annex-ops is the first platform image built from source on the VM
    shutil.copytree(ROOT / "ops", OUT / "ops",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))

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
    for svc in ("traefik", "authelia", "homepage"):
        h = hashlib.sha256()
        for f in sorted((OUT / "config" / svc).rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(OUT)).encode() + b"\0" + f.read_bytes())
        values[f"ANNEX_CONFIG_HASH_{svc.upper()}"] = h.hexdigest()[:12]

    (OUT / ".env").write_text(
        "# Generated by scripts/render.py. Non-secret settings only.\n"
        + "".join(f"{k}={v}\n" for k, v in values.items() if k not in TEMPLATE_ONLY)
    )
    (OUT / "VERSION").write_text(version_stamp(settings))
    print(f"render: {settings.name} -> {OUT.relative_to(ROOT)} (tls={mode}, home={values['ANNEX_URL_HOME']})")


if __name__ == "__main__":
    main()
