"""annex.yaml: load and validate.

Errors are collected, not raised one at a time, and each says what is wrong and
how to fix it, because coding agents read them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-]+$")
CRON_DESCRIPTORS = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
RESERVED_NAMES = {"home", "auth", "metabase", "logs", "gateway", "echo-test", "www", "api"}
INJECTED_ENV = {"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", "DATABASE_URL", "DATA_DIR",
                "ANNEX_GATEWAY_URL", "ANNEX_GATEWAY_TOKEN"}
TYPES = ("web", "job", "web+job")
KNOWN_KEYS = {"name", "owner", "description", "type", "port", "healthcheck", "access", "storage",
              "data", "schedule", "env", "secrets", "egress"}


class ManifestError(Exception):
    def __init__(self, path: Path, problems: list[str]):
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{path} is not valid ({len(problems)} problem(s)):\n{lines}\n"
                         "See the contract with: annex contract")


@dataclass
class Job:
    name: str
    cron: str
    command: list[str]


@dataclass
class Manifest:
    name: str
    owner: str
    description: str
    type: str
    port: int | None
    healthcheck: str | None
    postgres: bool
    files: bool
    views: list[str]
    schedule: list[Job]
    env: dict[str, str]
    secrets: list[str]
    egress: list[str] = field(default_factory=list)

    @property
    def is_web(self) -> bool:
        return self.type in ("web", "web+job")

    @property
    def has_jobs(self) -> bool:
        return self.type in ("job", "web+job")


def load(folder: Path) -> Manifest:
    folder = folder.resolve()
    path = folder / "annex.yaml"
    if not folder.is_dir():
        raise ManifestError(folder, [f"{folder} is not a folder. Pass the app folder that contains annex.yaml."])
    if not path.exists():
        raise ManifestError(path, ["annex.yaml is missing. Start from a template: annex init python-web <name>"])
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(path, [f"annex.yaml is not valid YAML: {e}"]) from None
    if not isinstance(raw, dict):
        raise ManifestError(path, ["annex.yaml must be a mapping of keys like name, owner, type"])

    p: list[str] = []
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        p.append(f"unknown key(s): {', '.join(unknown)}. Allowed keys: {', '.join(sorted(KNOWN_KEYS))}")

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        p.append(f"name: {name!r} is not valid. Use 2-32 characters: lowercase letters, digits and '-', "
                 "starting with a letter and not ending with '-', e.g. monthly-report")
    elif name in RESERVED_NAMES:
        p.append(f"name: {name!r} is reserved for the platform. Pick another name")
    owner = raw.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        p.append("owner: required. Set it to the builder's login, e.g. owner: sam")
    description = raw.get("description", "")
    if not isinstance(description, str):
        p.append("description: must be a short text")
        description = ""

    typ = raw.get("type")
    if typ not in TYPES:
        p.append(f"type: {typ!r} is not valid. Use one of: {', '.join(TYPES)}")
    web = typ in ("web", "web+job")
    jobs = typ in ("job", "web+job")

    port = raw.get("port")
    healthcheck = raw.get("healthcheck")
    if web:
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            p.append(f"port: {port!r} is not valid. Web apps need a port between 1024 and 65535 "
                     "(listen on 0.0.0.0:$PORT), e.g. port: 8080")
        if not isinstance(healthcheck, str) or not healthcheck.startswith("/"):
            p.append(f"healthcheck: {healthcheck!r} is not valid. Web apps need a path that returns 200 "
                     "when ready, e.g. healthcheck: /healthz")
    else:
        if port is not None or healthcheck is not None:
            p.append("port/healthcheck: only for type web or web+job. Remove them, or change type")
        port = healthcheck = None

    access = raw.get("access", "internal")
    if access != "internal":
        p.append(f"access: {access!r} is not supported. Only 'internal' (behind login) exists in v0")

    storage = raw.get("storage") or {}
    if not isinstance(storage, dict) or set(storage) - {"postgres", "files"}:
        p.append("storage: must be a mapping with optional postgres: true/false and files: true/false")
        storage = {}
    for k in ("postgres", "files"):
        if not isinstance(storage.get(k, False), bool):
            p.append(f"storage.{k}: must be true or false")

    data = raw.get("data") or {}
    views = data.get("views", []) if isinstance(data, dict) else None
    if not isinstance(views, list) or not all(isinstance(v, str) and re.match(r"^[a-z_][a-z0-9_]*$", v) for v in views):
        p.append("data.views: must be a list of view names, e.g. views: [client_summary]")
        views = []
    if isinstance(data, dict) and set(data) - {"views"}:
        p.append("data: only 'views' is supported")

    schedule_raw = raw.get("schedule") or []
    schedule: list[Job] = []
    if schedule_raw and not jobs:
        p.append("schedule: only for type job or web+job. Change type, or remove schedule")
    if jobs and not schedule_raw:
        p.append("schedule: type job/web+job needs at least one entry: "
                 "- {name: monthly, cron: \"0 6 1 * *\", command: [python, report.py]}")
    if not isinstance(schedule_raw, list):
        p.append("schedule: must be a list of {name, cron, command}")
        schedule_raw = []
    seen = set()
    for i, j in enumerate(schedule_raw):
        where = f"schedule[{i}]"
        if not isinstance(j, dict):
            p.append(f"{where}: must be a mapping with name, cron and command")
            continue
        jn, cron, cmd = j.get("name"), j.get("cron"), j.get("command")
        if not isinstance(jn, str) or not NAME_RE.match(jn):
            p.append(f"{where}.name: {jn!r} is not valid. Use lowercase letters, digits and '-', e.g. monthly")
        elif jn in seen:
            p.append(f"{where}.name: {jn!r} is used twice. Job names must be unique")
        seen.add(jn)
        if not valid_cron(cron):
            p.append(f"{where}.cron: {cron!r} is not valid. Use 5 fields (minute hour day month weekday), "
                     "e.g. \"0 6 1 * *\" for 06:00 on the 1st, or @daily / @hourly")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            p.append(f"{where}.command: must be a list of strings, e.g. [\"python\", \"report.py\"]")
        if not set(j) <= {"name", "cron", "command"}:
            p.append(f"{where}: unknown key(s) {', '.join(sorted(set(j) - {'name', 'cron', 'command'}))}")
        if isinstance(jn, str) and isinstance(cron, str) and isinstance(cmd, list):
            schedule.append(Job(jn, cron, [str(c) for c in cmd]))

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        p.append("env: must be a mapping of NAME: value (non-secret config only)")
        env = {}
    for k, v in env.items():
        if not isinstance(k, str) or not KEY_RE.match(k):
            p.append(f"env.{k}: names must be UPPER_SNAKE_CASE")
        elif k in INJECTED_ENV or k.startswith("ANNEX_"):
            p.append(f"env.{k}: is set by Annex; remove it from env")
        if not isinstance(v, (str, int, float, bool)):
            p.append(f"env.{k}: value must be a string or number")
    env = {str(k): str(v) for k, v in env.items()}

    secrets = raw.get("secrets") or []
    if not isinstance(secrets, list) or not all(isinstance(s, str) and KEY_RE.match(s) for s in secrets):
        p.append("secrets: must be a list of UPPER_SNAKE_CASE names, e.g. [SMTP_PASSWORD]. "
                 "Values are set by a human with: annex secret set <app> KEY=VALUE")
        secrets = []
    for s in secrets:
        if s in env:
            p.append(f"secrets: {s} is also in env. Keep secrets out of env")
        if s in INJECTED_ENV:
            p.append(f"secrets: {s} is set by Annex; pick another name")

    egress = raw.get("egress") or []
    if not isinstance(egress, list):
        p.append("egress: must be a list of hostnames (declared, not enforced in v0)")
        egress = []

    if not (folder / "Dockerfile").exists():
        p.append("Dockerfile is missing next to annex.yaml. Copy it from a template: annex init <template> <name>")

    if p:
        raise ManifestError(path, p)
    return Manifest(name=name, owner=owner.strip(), description=description.strip(), type=typ,
                    port=port, healthcheck=healthcheck, postgres=bool(storage.get("postgres")),
                    files=bool(storage.get("files")), views=list(views), schedule=schedule,
                    env=env, secrets=list(secrets), egress=[str(e) for e in egress])


def valid_cron(cron: object) -> bool:
    if not isinstance(cron, str):
        return False
    if cron in CRON_DESCRIPTORS or re.fullmatch(r"@every \d+[smh]", cron):
        return True
    fields = cron.split()
    return len(fields) == 5 and all(CRON_FIELD_RE.match(f) for f in fields)


def ofelia_schedule(cron: str) -> str:
    """Ofelia's cron has a seconds field; a 5-field spec gets '0' seconds."""
    return cron if cron.startswith("@") else f"0 {cron}"
