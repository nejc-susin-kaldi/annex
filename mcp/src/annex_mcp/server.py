"""annex MCP server (stdio).

Exposes the safe subset of the annex CLI to coding agents: contract, init,
deploy, list, status, logs, run, releases, rollback (without data). `remove`,
`secret set` and `rollback --with-data` stay human-only.
Paths are resolved against the agent's working directory if relative; prefer
absolute paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from annex_cli import ops
from annex_cli.manifest import ManifestError
from annex_cli.remote import AnnexError

server = MCPServer(
    name="annex",
    instructions=(
        "Annex deploys small internal web tools and jobs behind the client's login. "
        "Call annex_contract first and follow it. Workflow: annex_init a template into an empty folder, "
        "edit the code, annex_deploy the folder, then check the returned URL with annex_status/annex_logs. "
        "If a deploy fails, the error says exactly what to fix: fix it and deploy again. A deploy that fails its "
        "health check is rolled back automatically; annex_rollback switches back to an earlier release. "
        "Never write login code, Docker labels or database credentials. Secrets, app removal and restoring "
        "data are human-only: ask the user to run `annex secret set`, `annex remove` or "
        "`annex rollback --with-data`."
    ),
)


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


async def _call(fn, *args):
    """Run a blocking ops call in a worker thread; turn known errors into tool errors."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args))
    except (AnnexError, ManifestError) as e:
        # ToolError passes the message to the model; other exceptions would be hidden
        raise ToolError(str(e)) from None


@server.tool()
async def annex_contract() -> str:
    """The Annex app contract: annex.yaml fields, runtime rules, injected environment, gateway views,
    templates. Read this before creating or changing an app."""
    return ops.contract()


@server.tool()
async def annex_init(template: str, name: str, directory: str, owner: str) -> str:
    """Copy an app template into `directory` (must be empty or not exist).
    template: one of python-web, python-job, node-web, static.
    name: the app name and hostname (lowercase letters, digits, '-').
    directory: absolute path of the app folder to create.
    owner: the builder's login, shown on the discovery page (ask the user if unknown).
    Returns the folder path and the template's AGENTS.md, which explains what to edit."""
    dest = await _call(ops.init, template, name, Path(directory).expanduser(), owner)
    agents = (dest / "AGENTS.md").read_text() if (dest / "AGENTS.md").exists() else ""
    files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    return f"Created {dest} with files: {', '.join(files)}\n\n{agents}"


@server.tool()
async def annex_deploy(path: str) -> str:
    """Validate, provision (database, files, gateway access), build, run and register the app in the
    folder at `path` (absolute). Takes 15-90 seconds. Each deploy is a numbered release; if the new
    release fails its health check, Annex switches back to the previous one automatically and the error
    includes the new release's last log lines. Returns the app's URL, release and injected environment,
    or an error explaining what to fix."""
    steps: list[str] = []
    res = await _call(ops.deploy, Path(path).expanduser(), steps.append, "mcp")
    return _dump({**res, "steps": steps})


@server.tool()
async def annex_list() -> str:
    """List deployed apps with owner, type, state, health and URL."""
    return _dump(await _call(ops.list_apps))


@server.tool()
async def annex_status(app: str) -> str:
    """An app's state, health, image, storage, views, schedule and recent log lines."""
    return _dump(await _call(ops.status, app))


@server.tool()
async def annex_logs(app: str, tail: int = 100, since: str | None = None) -> str:
    """An app's recent stdout/stderr. tail: number of lines; since: e.g. '10m' or '2h'."""
    return await _call(ops.logs, app, tail, since)


@server.tool()
async def annex_releases(app: str) -> str:
    """An app's releases, newest first: result, when, who, git commit, and which can be rolled back to."""
    return _dump(await _call(ops.releases, app))


@server.tool()
async def annex_rollback(app: str, to_release: int | None = None) -> str:
    """Switch an app back to an earlier release (default: the one before the current), re-applying that
    release's manifest (views, storage, schedule). Code and config only: the database and /data are left
    as they are. Restoring data too is human only (`annex rollback <app> --with-data`)."""
    steps: list[str] = []
    res = await _call(ops.rollback, app, to_release, False, steps.append, "mcp")
    return _dump({**res, "steps": steps})


@server.tool()
async def annex_run(app: str, job: str) -> str:
    """Run one of an app's scheduled jobs now (by its name in annex.yaml) and return its output
    and exit code."""
    return _dump(await _call(ops.run_job, app, job))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
