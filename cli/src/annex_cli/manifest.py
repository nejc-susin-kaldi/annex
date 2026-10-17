"""annex.yaml: load and validate.

Errors are collected, not raised one at a time, and each says what is wrong and
how to fix it, because coding agents read them.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-]+$")
CRON_DESCRIPTORS = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
# mail and ops exist as of M1.2; status, hooks and console are taken for M1.3, M1.5 and M2
RESERVED_NAMES = {"home", "auth", "metabase", "logs", "gateway", "mail", "ops", "status", "hooks", "console",
                  "echo-test", "www", "api"}
INJECTED_ENV = {"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", "DATABASE_URL", "DATA_DIR",
                "ANNEX_GATEWAY_URL", "ANNEX_GATEWAY_TOKEN", "ANNEX_OPS_URL", "ANNEX_OPS_TOKEN", "ANNEX_HOOK_TOKEN",
                "ANNEX_HOOKS_URL", "LOG_LEVEL"}
# injected with a default, but the app may set its own value under env: (unlike the other injected names)
OVERRIDABLE_ENV = {"LOG_LEVEL": ("debug", "info", "warn", "error")}
HOOK_VERIFY = ("trello", "stripe", "github", "slack", "shared-secret")
MAX_WEBHOOKS = 10
TYPES = ("web", "job", "web+job")
KNOWN_KEYS = {"name", "owner", "description", "type", "port", "healthcheck", "access", "storage",
              "data", "notify", "alerts", "webhooks", "schedule", "env", "secrets", "egress", "resources"}
MEMORY_DEFAULT_MB = 256
MEMORY_MIN_MB, MEMORY_MAX_MB = 128, 512
MEMORY_RE = re.compile(r"^(\d+)\s*[mM]$")
LOGIN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
SLACK_CHANNEL_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,79}$")  # same rule in vm/annex_vm.py and annex-ops


class ManifestError(Exception):
    def __init__(self, path: Path, problems: list[str]):
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        topics = [t for t in ("notify", "webhooks") if any(p.startswith(t) for p in problems)]
        more = "".join(f" and: annex contract {t}" for t in topics)
        super().__init__(f"{path} is not valid ({len(problems)} problem(s)):\n{lines}\n"
                         f"See the contract with: annex contract{more}")


@dataclass
class Job:
    name: str
    cron: str
    command: list[str]


@dataclass
class Webhook:
    name: str     # part of the public URL: https://hooks.<domain>/h/<app>/<name>
    path: str     # the app's internal endpoint (POST)
    verify: str   # how annex-ops checks the provider's signature
    secret: str   # a name from `secrets:`


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
    # defaulted: manifests stored with older releases don't have it (rollback loads them)
    memory_mb: int = MEMORY_DEFAULT_MB
    # defaulted for the same reason: a rollback to a release without notify: must revoke the grant
    notify_email: bool = False
    notify_slack: list[str] = field(default_factory=list)
    # who hears about this app's problems, besides the admins: "owner", logins and #channels (older releases: owner)
    alerts_to: list[str] = field(default_factory=lambda: ["owner"])
    # defaulted (and rebuilt in from_dict) so manifests stored with older releases load for rollback
    webhooks: list[Webhook] = field(default_factory=list)

    @property
    def is_web(self) -> bool:
        return self.type in ("web", "web+job")

    @property
    def has_jobs(self) -> bool:
        return self.type in ("job", "web+job")

    def to_dict(self) -> dict:
        """The validated manifest as plain data, as stored with each release."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Manifest:
        return cls(**{**d, "schedule": [Job(**j) for j in d.get("schedule", [])],
                      "webhooks": [Webhook(**w) for w in d.get("webhooks", [])]})


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
        elif k in OVERRIDABLE_ENV:
            if str(v).strip().lower() not in OVERRIDABLE_ENV[k]:
                p.append(f"env.{k}: {v!r} is not valid. Use one of: {', '.join(OVERRIDABLE_ENV[k])} (the default is info)")
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

    notify = raw.get("notify") or {}
    notify_email, notify_slack = False, []
    if not isinstance(notify, dict) or set(notify) - {"email", "slack"}:
        p.append("notify: must be a mapping with only email and slack, e.g. notify: {email: true, slack: [\"#finance\"]}")
    else:
        notify_email = notify.get("email", False)
        if not isinstance(notify_email, bool):
            p.append(f"notify.email: {notify_email!r} is not valid. Use true or false")
            notify_email = False
        slack = notify.get("slack", [])
        if not isinstance(slack, list) or not all(isinstance(c, str) for c in slack):
            p.append("notify.slack: must be a list of channel names, e.g. [\"#finance-reports\"]")
        else:
            for c in slack:
                if not SLACK_CHANNEL_RE.match(c):
                    hint = f"{c!r} must start with '#', e.g. '#{c.lstrip('#')}'" if not c.startswith("#") else \
                        f"{c!r} is not a valid channel name. Use lowercase letters, digits, '-' and '_', e.g. '#finance-reports'"
                    p.append(f"notify.slack: {hint}")
            if len(set(slack)) != len(slack):
                p.append("notify.slack: a channel is listed twice. List each channel once")
            notify_slack = list(slack)

    wh_raw = raw.get("webhooks") or []
    webhooks: list[Webhook] = []
    if not isinstance(wh_raw, list):
        p.append("webhooks: must be a list of {name, path, verify, secret}")
        wh_raw = []
    if wh_raw and not web:
        p.append("webhooks: only for type web or web+job (the provider's call is forwarded to the app's HTTP endpoint). "
                 "Change type, or remove webhooks")
    if len(wh_raw) > MAX_WEBHOOKS:
        p.append(f"webhooks: {len(wh_raw)} is too many; the limit is {MAX_WEBHOOKS} per app")
    seen_hooks = set()
    for i, w in enumerate(wh_raw[:MAX_WEBHOOKS]):
        where = f"webhooks[{i}]"
        if not isinstance(w, dict):
            p.append(f"{where}: must be a mapping with name, path, verify and secret")
            continue
        if set(w) - {"name", "path", "verify", "secret"}:
            p.append(f"{where}: unknown key(s) {', '.join(sorted(set(w) - {'name', 'path', 'verify', 'secret'}))}. "
                     "Allowed: name, path, verify, secret")
        hn, hp, hv, hs = w.get("name"), w.get("path"), w.get("verify"), w.get("secret")
        if not isinstance(hn, str) or not NAME_RE.match(hn):
            p.append(f"{where}.name: {hn!r} is not valid. Use 2-32 lowercase letters, digits and '-', e.g. trello "
                     "(it becomes part of the public URL)")
        elif hn in seen_hooks:
            p.append(f"{where}.name: {hn!r} is used twice. Webhook names must be unique")
        seen_hooks.add(hn)
        if not isinstance(hp, str) or not re.match(r"^/[A-Za-z0-9._~/-]*$", hp):
            p.append(f"{where}.path: {hp!r} is not valid. Give the app's own endpoint, starting with '/', e.g. /hooks/trello")
        elif hp == healthcheck:
            p.append(f"{where}.path: {hp!r} is the health check path. Use a different endpoint for the webhook")
        if hv not in HOOK_VERIFY:
            p.append(f"{where}.verify: {hv!r} is not valid. Use one of: {', '.join(HOOK_VERIFY)}")
        if not isinstance(hs, str) or hs not in secrets:
            p.append(f"{where}.secret: {hs!r} must be a name listed under secrets:, e.g. secrets: [TRELLO_SECRET] "
                     f"and secret: TRELLO_SECRET. A human sets its value with: annex secret set {name or '<app>'} KEY=VALUE")
        if all(isinstance(x, str) for x in (hn, hp, hv, hs)):
            webhooks.append(Webhook(hn, hp, hv, hs))

    alerts = raw.get("alerts") or {}
    alerts_to = ["owner"]
    if not isinstance(alerts, dict) or set(alerts) - {"to"}:
        p.append("alerts: must be a mapping with only 'to', e.g. alerts: {to: [owner, cm, \"#finance-alerts\"]}")
    elif "to" in alerts:
        to = alerts["to"]
        if not isinstance(to, list) or not to or not all(isinstance(t, str) for t in to):
            p.append("alerts.to: must be a non-empty list of 'owner', logins and Slack channels, "
                     "e.g. [owner, cm, \"#finance-alerts\"]")
        else:
            for t in to:
                if t == "owner" or LOGIN_RE.match(t) or SLACK_CHANNEL_RE.match(t):
                    continue
                if t.startswith("#"):
                    p.append(f"alerts.to: {t!r} is not a valid Slack channel. Use lowercase letters, digits, '-' and "
                             "'_', e.g. '#finance-alerts'")
                else:
                    p.append(f"alerts.to: {t!r} is not a login. Use 'owner', a login like 'cm' (lowercase letters, "
                             "digits, '.', '_' or '-'), or a Slack channel like '#finance-alerts'")
            if len(set(to)) != len(to):
                p.append("alerts.to: an entry is listed twice. List each once")
            alerts_to = list(to)

    resources = raw.get("resources") or {}
    memory_mb = MEMORY_DEFAULT_MB
    if not isinstance(resources, dict) or set(resources) - {"memory"}:
        p.append("resources: must be a mapping with only memory, e.g. resources: {memory: 512m}")
    elif "memory" in resources:
        mem = resources["memory"]
        mm = MEMORY_RE.match(str(mem)) if isinstance(mem, str) else None
        if not mm or not MEMORY_MIN_MB <= int(mm.group(1)) <= MEMORY_MAX_MB:
            p.append(f"resources.memory: {mem!r} is not valid. Use megabytes with an 'm' between "
                     f"{MEMORY_MIN_MB}m and {MEMORY_MAX_MB}m, e.g. memory: 512m. The default is "
                     f"{MEMORY_DEFAULT_MB}m; if the app needs more than {MEMORY_MAX_MB}m, it's too big for Annex: "
                     "ask the platform team")
        else:
            memory_mb = int(mm.group(1))

    if not (folder / "Dockerfile").exists():
        p.append("Dockerfile is missing next to annex.yaml. Copy it from a template (annex init <template> <name>), "
                 "or write your own: see: annex contract stack")

    if p:
        raise ManifestError(path, p)
    return Manifest(name=name, owner=owner.strip(), description=description.strip(), type=typ,
                    port=port, healthcheck=healthcheck, postgres=bool(storage.get("postgres")),
                    files=bool(storage.get("files")), views=list(views), schedule=schedule,
                    env=env, secrets=list(secrets), egress=[str(e) for e in egress], memory_mb=memory_mb,
                    notify_email=notify_email, notify_slack=notify_slack,
                    alerts_to=alerts_to, webhooks=webhooks)


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
