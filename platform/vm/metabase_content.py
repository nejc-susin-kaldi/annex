#!/usr/bin/env python3
"""Goat Arcade's Metabase content as code (GA.3, D109). Runs ON THE VM, called by vm/metabase-setup.sh after it has
signed in as the Metabase admin. Stdlib only.

From the specs in $METABASE_SPEC_DIR/goat-arcade (default /srv/annex/platform/config/metabase/goat-arcade):
  model.json        the connection's column types and names (semantic types, keys, EUR, coordinates)
  collections.json  the Goat Arcade collections, who may view and edit them, who gets the SQL editor
  dashboards/*.json one dashboard each: its cards (native SQL over annex_api views), layout and filters

Idempotent: everything is compared with what Metabase has and only differences are written; each line printed is a
change, a warning or the check's summary. Dashboards and cards are found again by the key in their description
("Kept as code by Annex [<key>]"), so a rename in the spec renames, and nothing is ever duplicated. Every card is run
at the end (POST /api/card/:id/query) and its row count reported; a failing or empty card is a warning.

Environment: MB_CONTAINER (the Metabase container id), MB_SESSION (an admin session), METABASE_SPEC_DIR,
METABASE_VERBOSE=1 (one line per card in the check).
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid

MARK = "Kept as code by Annex [{key}]: changes made here are overwritten at the next push."
MARK_RE = re.compile(r"Kept as code by Annex \[([A-Za-z0-9._-]+)\]")
TAG_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
DISPLAYS = {"scalar", "smartscalar", "line", "bar", "area", "row", "pie", "table", "map", "combo"}
NS = uuid.UUID("6f1c2b8e-4f4e-4d0b-9a37-61a3c6d0a903")  # fixed namespace: stable template-tag and parameter ids


class MBError(Exception):
    pass


def log(msg):
    print(f"metabase: {msg}", flush=True)


# ---------------------------------------------------------------- HTTP (curl inside the Metabase container)

class MB:
    def __init__(self, container, session):
        self.container, self.session = container, session

    def call(self, method, path, body=None):
        cmd = ["docker", "exec", "-i", self.container, "curl", "-sS", "--max-time", "180", "-X", method,
               "-H", "Content-Type: application/json", "-H", f"X-Metabase-Session: {self.session}",
               "-w", "\n%{http_code}"]
        if body is not None:
            cmd += ["--data-binary", "@-"]
        cmd.append("http://127.0.0.1:3000" + path)
        p = subprocess.run(cmd, input=json.dumps(body).encode() if body is not None else b"", capture_output=True)
        out = p.stdout.decode()
        text, _, code = out.rpartition("\n")
        if p.returncode or not code.isdigit() or not 200 <= int(code) < 300:
            raise MBError(f"{method} {path}: HTTP {code or '?'} {(text or p.stderr.decode())[:500]}")
        return json.loads(text) if text.strip() else None

    def get(self, path):
        return self.call("GET", path)


# ---------------------------------------------------------------- specs (pure)

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sql_text(sql):
    """A card's SQL: one string, or a list of lines (JSON has no multi-line strings)."""
    return "\n".join(sql) if isinstance(sql, list) else sql


def check_sql(sql, views, schema="annex_api"):
    """Problems with a card's SQL: it may read only the curated views (schema.view), CTEs and subqueries."""
    problems = []
    low = sql.lower()
    for bad in ("src_arcade", "pg_catalog", "information_schema", "annex_gateway", "public."):
        if bad in low:
            problems.append(f"mentions {bad}: cards read only {schema} views")
    ctes = {m.group(1).lower() for m in re.finditer(r"(?i)\b([a-z_][a-z0-9_]*)\s+as\s+(?:materialized\s+)?\(", sql)}
    for m in re.finditer(r"(?i)\b(?:from|join)\s+([a-z_\"][a-z0-9_.\"]*)(\s*\()?", sql):
        name, call = m.group(1).replace('"', "").lower(), m.group(2)
        if call:  # a set-returning function such as generate_series(...)
            if name not in ("generate_series", "unnest"):
                problems.append(f"reads from function {name}()")
            continue
        if "." in name:
            sch, _, view = name.partition(".")
            if sch != schema or view not in views:
                problems.append(f"reads {name}: not one of the {schema} views")
        elif name not in ctes and name not in ("lateral",):
            problems.append(f"reads {name} without the {schema}. prefix (and it is no CTE)")
    return problems


def validate_dashboard(spec, views, teams, fname):
    """Errors in one dashboard spec, as text that says what to fix."""
    errs = []
    where = f"{fname}"
    for k in ("key", "name", "collection", "cards"):
        if k not in spec:
            errs.append(f"{where}: missing '{k}'")
    if errs:
        return errs
    if spec["collection"] not in teams:
        errs.append(f"{where}: collection '{spec['collection']}' is not a team in collections.json ({', '.join(teams)})")
    slugs = [p["slug"] for p in spec.get("parameters", [])]
    keys = set()
    for c in spec["cards"]:
        cw = f"{where} card '{c.get('key', '?')}'"
        for k in ("key", "name", "display", "sql", "pos"):
            if k not in c:
                errs.append(f"{cw}: missing '{k}'")
        if any(k not in c for k in ("key", "sql", "display", "pos")):
            continue
        if c["key"] in keys:
            errs.append(f"{cw}: duplicate key")
        keys.add(c["key"])
        if c["display"] not in DISPLAYS:
            errs.append(f"{cw}: display '{c['display']}' is not one of {sorted(DISPLAYS)}")
        if not (isinstance(c["pos"], list) and len(c["pos"]) == 4 and c["pos"][0] + c["pos"][2] <= 24):
            errs.append(f"{cw}: pos must be [col, row, width, height] within 24 columns")
        sql = sql_text(c["sql"])
        errs += [f"{cw}: {p}" for p in check_sql(sql, views)]
        tags = set(TAG_RE.findall(sql))
        for t in tags - set(slugs):
            errs.append(f"{cw}: uses {{{{{t}}}}} but the dashboard has no parameter with slug '{t}'")
        for p in spec.get("parameters", []):
            if p.get("every_card") and p["slug"] not in tags:
                errs.append(f"{cw}: the '{p['slug']}' filter applies to every card: add [[AND <column> = {{{{{p['slug']}}}}}]]")
    return errs


def stable_id(*parts, n=None):
    u = str(uuid.uuid5(NS, "/".join(parts)))
    return u[:n] if n else u


def title_of(alias):
    """Column header from a SQL alias: revenue_30d_eur -> Revenue 30d."""
    base = alias[:-4] if alias.endswith("_eur") else alias
    t = base.replace("_", " ").strip()
    return t[:1].upper() + t[1:]


def card_viz(card, cols, currency="EUR"):
    """The card's visualization settings: the spec's, plus a title for every column and EUR on every *_eur one."""
    viz = json.loads(json.dumps(card.get("viz", {})))
    cs = viz.setdefault("column_settings", {})
    for name in cols:
        key = json.dumps(["name", name], separators=(",", ":"))
        s = cs.setdefault(key, {})
        s.setdefault("column_title", card.get("titles", {}).get(name, title_of(name)))
        if name.endswith("_eur"):
            s.setdefault("number_style", "currency")
            s.setdefault("currency", currency)
            s.setdefault("currency_style", "symbol")
    if not cs:
        viz.pop("column_settings")
    return viz


def card_tags(dash_key, sql, params):
    by_slug = {p["slug"]: p for p in params}
    tags = {}
    for t in sorted(set(TAG_RE.findall(sql))):
        tags[t] = {"id": stable_id(dash_key, "tag", t), "name": t, "display-name": by_slug[t]["name"],
                   "type": "text", "required": False}
    return tags


def native_of(dq):
    """(sql, template tags) from a stored dataset_query, legacy or MBQL 5 (stages)."""
    if not dq:
        return None, {}
    if "native" in dq and isinstance(dq["native"], dict):
        return dq["native"].get("query"), dq["native"].get("template-tags") or {}
    st = (dq.get("stages") or [{}])[0]
    tags = st.get("template-tags") or {}
    if isinstance(tags, list):  # MBQL 5 keeps them as a list of tags
        tags = {t["name"]: t for t in tags}
    return st.get("native"), tags


def tags_norm(tags):
    return {k: {f: v.get(f) for f in ("id", "name", "display-name", "type", "required")} for k, v in tags.items()}


def param_norm(p):
    keep = ("id", "name", "slug", "type", "sectionId", "values_source_type", "values_source_config", "values_query_type")
    return {k: p.get(k) for k in keep if p.get(k) is not None}


def dashcards_norm(dcs):
    out = []
    for d in dcs:
        maps = sorted((m["parameter_id"], json.dumps(m["target"])) for m in d.get("parameter_mappings") or [])
        out.append((d.get("card_id") or 0, d["row"], d["col"], d["size_x"], d["size_y"], tuple(maps)))
    return sorted(out)


def field_changes(model, metadata):
    """What to change on the Goat Arcade fields: [(field_id, 'view.column', {changes})]. Pure."""
    views = set(model["views"])
    tables = {t["name"]: t for t in metadata["tables"] if t.get("schema") == model["schema"] and t["name"] in views}
    fid = {(t, f["name"]): f["id"] for t, tt in tables.items() for f in tt["fields"]}
    keys, aliases = model["keys"], model.get("key_aliases", {})
    out = []
    for tname, t in sorted(tables.items()):
        for f in t["fields"]:
            name = f["name"]
            if "→" in name:  # Metabase's unfolded JSON sub-fields (opening_hours → mon)
                continue
            want = {}
            key = aliases.get(name, name)
            if key in keys:
                home = keys[key]
                if tname == home and name == key:
                    want["semantic_type"] = "type/PK"
                    want["fk_target_field_id"] = None
                elif (home, key) in fid:
                    want["semantic_type"] = "type/FK"
                    want["fk_target_field_id"] = fid[(home, key)]
            elif name in model.get("coordinates", {}):
                want["semantic_type"] = model["coordinates"][name]
            elif name.endswith(model.get("currency_suffix", "_eur")):
                want["semantic_type"] = "type/Currency"
                settings = dict(f.get("settings") or {})
                if settings.get("currency") != model["currency"]:
                    settings["currency"] = model["currency"]
                    want["settings"] = settings
            if name in model.get("fields", {}):
                want["display_name"] = model["fields"][name]
            changes = {k: v for k, v in want.items() if f.get(k) != v}
            if changes:
                out.append((f["id"], f"{tname}.{name}", changes))
    return out


def table_changes(model, metadata):
    out = []
    for t in metadata["tables"]:
        want = model.get("tables", {}).get(t["name"])
        if t.get("schema") == model["schema"] and want and t.get("display_name") != want:
            out.append((t["id"], t["name"], {"display_name": want}))
    return out


def data_perm_changes(graph, db_id, group_ids, query_builder, native):
    """Groups whose create-queries on db_id must change: {gid: new entry}. Pure."""
    want = {}
    for g in query_builder:
        want[group_ids[g]] = "query-builder"
    for g in native:
        want[group_ids[g]] = "query-builder-and-native"
    out = {}
    for gid, cq in want.items():
        cur = ((graph.get("groups") or {}).get(str(gid)) or {}).get(str(db_id)) or {}
        if cur.get("create-queries") != cq or cur.get("view-data") != "unrestricted":
            entry = dict(cur)
            entry["view-data"] = "unrestricted"
            entry["create-queries"] = cq
            out[str(gid)] = {str(db_id): entry}
    return out


def collection_perm_changes(graph, wanted):
    """wanted: {gid: {collection_id: 'read'|'write'}} -> the part of it that differs from the graph. Pure."""
    out = {}
    for gid, colls in wanted.items():
        cur = (graph.get("groups") or {}).get(str(gid)) or {}
        diff = {str(c): p for c, p in colls.items() if cur.get(str(c)) != p}
        if diff:
            out[str(gid)] = diff
    return out


# ---------------------------------------------------------------- steps against Metabase

def database_id(mb, name):
    for d in mb.get("/api/database")["data"]:
        if d["name"] == name:
            return d["id"]
    raise MBError(f"no Metabase database named '{name}' (vm/metabase-setup.sh creates or renames it first)")


def sync_and_wait(mb, db_id, model, timeout=300):
    """Ask for a schema sync, then wait until every view in model.json is there with its columns."""
    mb.call("POST", f"/api/database/{db_id}/sync_schema", {})
    deadline, waited = time.time() + timeout, False
    while True:
        meta = mb.get(f"/api/database/{db_id}/metadata?include_hidden=true")
        have = {t["name"] for t in meta["tables"] if t.get("schema") == model["schema"] and t.get("fields")}
        missing = sorted(set(model["views"]) - have)
        if not missing:
            if waited:
                log(f"{model['database']}: synced, all {len(model['views'])} views there")
            return meta
        if time.time() > deadline:
            raise MBError(f"after {timeout} s of sync these views are still missing in Metabase: {', '.join(missing)}. "
                          f"Check they exist in product-db (scripts/democlient.sh status) and Metabase's log.")
        waited = True
        time.sleep(3)


def apply_model(mb, db_id, model, meta):
    n = 0
    for tid, name, ch in table_changes(model, meta):
        mb.call("PUT", f"/api/table/{tid}", ch)
        log(f"table {name}: display name '{ch['display_name']}'")
        n += 1
    for fid, name, ch in field_changes(model, meta):
        mb.call("PUT", f"/api/field/{fid}", ch)
        what = ", ".join(f"{k}={v}" for k, v in ch.items() if k != "settings")
        if "settings" in ch:
            what += (", " if what else "") + f"currency={ch['settings'].get('currency')}"
        log(f"field {name}: {what}")
        n += 1
    return n


def groups_by_name(mb):
    return {g["name"]: g["id"] for g in mb.get("/api/permissions/group")}


def ensure_collection(mb, colls, name, parent_id, description):
    loc = "/" if parent_id is None else f"/{parent_id}/"
    for c in colls:
        if c.get("name") == name and c.get("location") == loc and not c.get("personal_owner_id") \
                and not c.get("archived"):
            if (c.get("description") or "") != description:
                mb.call("PUT", f"/api/collection/{c['id']}", {"description": description})
                log(f"collection {name}: description updated")
            return c["id"]
    body = {"name": name, "description": description}
    if parent_id is not None:
        body["parent_id"] = parent_id
    c = mb.call("POST", "/api/collection", body)
    log(f"created collection {name}" + ("" if parent_id is None else " (in Goat Arcade)"))
    return c["id"]


def apply_collections(mb, spec, gids):
    colls = mb.get("/api/collection")
    parent = ensure_collection(mb, colls, spec["parent"]["name"], None, spec["parent"]["description"])
    team_ids = {}
    for t in spec["teams"]:
        team_ids[t["name"]] = ensure_collection(mb, colls, t["name"], parent, t["description"])
    everyone = gids["All Users"]
    wanted = {everyone: {parent: "read", **{c: "read" for c in team_ids.values()}}}
    for t in spec["teams"]:
        g = gids.get(t["group"])
        if g is None:
            raise MBError(f"group '{t['group']}' (collections.json) does not exist: add it to people.json's groups")
        wanted.setdefault(g, {})[team_ids[t["name"]]] = "write"
    graph = mb.get("/api/collection/graph")
    diff = collection_perm_changes(graph, wanted)
    if diff:
        mb.call("PUT", "/api/collection/graph", {"revision": graph["revision"], "groups": diff})
        names = {v: k for k, v in gids.items()}
        log("collection permissions: " + "; ".join(
            f"{names.get(int(g), g)} " + ", ".join(f"{p} on {c}" for c, p in d.items()) for g, d in diff.items()))
    return parent, team_ids


def apply_data_perms(mb, db_id, spec, gids):
    missing = [g for g in spec["query_builder"] + spec["native_sql"] if g not in gids]
    if missing:
        raise MBError(f"groups {missing} (collections.json) do not exist: add them to people.json's groups")
    graph = mb.get("/api/permissions/graph")
    diff = data_perm_changes(graph, db_id, gids, spec["query_builder"], spec["native_sql"])
    if diff:
        mb.call("PUT", "/api/permissions/graph", {"revision": graph["revision"], "groups": diff})
        names = {v: k for k, v in gids.items()}
        log("data permissions: " + "; ".join(
            f"{names.get(int(g), g)} {e[str(db_id)]['create-queries']}" for g, e in diff.items()))


def run_sql(mb, db_id, sql, tags=None):
    """Run a native query (template tags left empty, so optional clauses drop out); (cols, rows) or MBError."""
    r = mb.call("POST", "/api/dataset", {"database": db_id, "type": "native",
                                         "native": {"query": sql, "template-tags": tags or {}}})
    if r.get("status") != "completed" or r.get("error"):
        raise MBError(str(r.get("error") or r.get("status"))[:400])
    return [c["name"] for c in r["data"]["cols"]], r["data"]["rows"]


def managed_dashboards(mb, coll_ids):
    found = {}
    for cid in coll_ids:
        for it in mb.get(f"/api/collection/{cid}/items?models=dashboard").get("data", []):
            m = MARK_RE.search(it.get("description") or "")
            if m:
                found.setdefault(m.group(1), it["id"])
    return found


def with_mark(text, key):
    return ((text or "").strip() + "\n\n" + MARK.format(key=key)).strip()


def apply_dashboard(mb, db_id, spec, coll_id, existing_id):
    key = spec["key"]
    params, keep_params = [], False
    for p in spec.get("parameters", []):
        par = {"id": stable_id(key, "param", p["slug"], n=8), "name": p["name"], "slug": p["slug"],
               "type": "string/=", "sectionId": "string"}
        if p.get("values_sql"):
            try:
                _, rows = run_sql(mb, db_id, sql_text(p["values_sql"]))
            except MBError as e:  # e.g. the client's database is down: keep the filter as it is
                log(f"WARNING: the values of filter '{p['name']}' on '{spec['name']}' could not be read, kept: {e}")
                keep_params = True
                rows = []
            par.update({"values_query_type": "list", "values_source_type": "static-list",
                        "values_source_config": {"values": [[r[0], r[1]] if len(r) > 1 else [r[0]] for r in rows]}})
        params.append(par)
    want = {"name": spec["name"], "description": with_mark(spec.get("description"), key), "collection_id": coll_id,
            "width": spec.get("width", "full")}
    if existing_id is None:
        d = mb.call("POST", "/api/dashboard", {**want, "parameters": params})
        existing_id = d["id"]
        log(f"created dashboard '{spec['name']}'")
    dash = mb.get(f"/api/dashboard/{existing_id}")
    upd = {k: v for k, v in want.items() if dash.get(k) != v}
    if keep_params and dash.get("parameters"):
        params = dash["parameters"]
    elif [param_norm(p) for p in dash.get("parameters") or []] != [param_norm(p) for p in params]:
        upd["parameters"] = params
    if upd:
        mb.call("PUT", f"/api/dashboard/{existing_id}", upd)
        log(f"dashboard '{spec['name']}': updated {', '.join(sorted(upd))}")
        dash = mb.get(f"/api/dashboard/{existing_id}")

    # cards: found by key among the dashboard's own cards
    have = {}
    for dc in dash.get("dashcards") or []:
        c = dc.get("card") or {}
        m = MARK_RE.search(c.get("description") or "")
        if m and not c.get("archived"):
            have.setdefault(m.group(1), c)
    placed = []
    for c in spec["cards"]:
        ckey = f"{key}.{c['key']}"
        sql = sql_text(c["sql"])
        tags = card_tags(key, sql, params)
        try:
            cols, _ = run_sql(mb, db_id, sql, tags)
        except MBError as e:
            log(f"WARNING: card '{c['name']}' ({ckey}) does not run, left as it was: {e}")
            if ckey in have:
                placed.append((c, have[ckey]["id"]))
            continue
        body = {"name": c["name"], "description": with_mark(c.get("description"), ckey), "display": c["display"],
                "collection_id": coll_id, "visualization_settings": card_viz(c, cols),
                "dataset_query": {"database": db_id, "type": "native",
                                  "native": {"query": sql, "template-tags": tags}}}
        old = have.get(ckey)
        if old is None:
            new = mb.call("POST", "/api/card", {**body, "dashboard_id": existing_id})
            log(f"created card '{c['name']}' on '{spec['name']}'")
            placed.append((c, new["id"]))
            continue
        cur = mb.get(f"/api/card/{old['id']}")
        q, t = native_of(cur.get("dataset_query"))
        upd = {k: body[k] for k in ("name", "description", "display", "collection_id", "visualization_settings")
               if cur.get(k) != body[k]}
        if q != sql or tags_norm(t) != tags_norm(tags) or cur.get("database_id") != db_id:
            upd["dataset_query"] = body["dataset_query"]
        if upd:
            mb.call("PUT", f"/api/card/{old['id']}", upd)
            log(f"card '{c['name']}' on '{spec['name']}': updated {', '.join(sorted(upd))}")
        placed.append((c, old["id"]))

    # layout and filter wiring
    dash = mb.get(f"/api/dashboard/{existing_id}")
    by_card = {}
    for dc in dash.get("dashcards") or []:
        by_card.setdefault(dc["card_id"], dc["id"])
    want_dcs, neg = [], -1
    for c, card_id in placed:
        sql = sql_text(c["sql"])
        maps = [{"parameter_id": p["id"], "card_id": card_id, "target": ["variable", ["template-tag", p["slug"]]]}
                for p in params if p["slug"] in TAG_RE.findall(sql)]
        col, row, w, h = c["pos"]
        dc_id = by_card.get(card_id)
        if dc_id is None:
            dc_id, neg = neg, neg - 1
        want_dcs.append({"id": dc_id, "card_id": card_id, "row": row, "col": col, "size_x": w, "size_y": h,
                         "parameter_mappings": maps, "visualization_settings": {}})
    if dashcards_norm(dash.get("dashcards") or []) != dashcards_norm(want_dcs):
        mb.call("PUT", f"/api/dashboard/{existing_id}", {"dashcards": want_dcs, "tabs": dash.get("tabs") or []})
        log(f"dashboard '{spec['name']}': layout and filters set ({len(want_dcs)} cards)")

    return existing_id, placed


def check_cards(mb, spec, placed, verbose):
    """Run every card as Metabase would; a summary line per dashboard, a warning per failing or empty card."""
    counts, bad = [], 0
    for c, card_id in placed:
        try:
            r = mb.call("POST", f"/api/card/{card_id}/query", {})
            if r.get("status") != "completed" or r.get("error"):
                raise MBError(str(r.get("error") or r.get("status"))[:300])
            n = r.get("row_count", len(r["data"]["rows"]))
        except MBError as e:
            log(f"WARNING: card '{c['name']}' on '{spec['name']}' fails: {e}")
            bad += 1
            continue
        if n == 0:
            log(f"WARNING: card '{c['name']}' on '{spec['name']}' returns no rows")
            bad += 1
        if verbose:
            log(f"  check {spec['key']}.{c['key']} '{c['name']}': {n} rows")
        counts.append(f"{c['key']} {n}")
    log(f"checked '{spec['name']}': {len(counts)} of {len(spec['cards'])} cards ran (rows: {', '.join(counts)})")
    return bad


def main():
    spec_dir = os.path.join(os.environ.get("METABASE_SPEC_DIR", "/srv/annex/platform/config/metabase"), "goat-arcade")
    mb = MB(os.environ["MB_CONTAINER"], os.environ["MB_SESSION"])
    verbose = os.environ.get("METABASE_VERBOSE") == "1"
    model = load_json(os.path.join(spec_dir, "model.json"))
    colls = load_json(os.path.join(spec_dir, "collections.json"))
    ddir = os.path.join(spec_dir, "dashboards")
    dashboards = [(f, load_json(os.path.join(ddir, f))) for f in sorted(os.listdir(ddir)) if f.endswith(".json")]
    teams = [t["name"] for t in colls["teams"]]
    errs = [e for f, d in dashboards for e in validate_dashboard(d, set(model["views"]), teams, f"dashboards/{f}")]
    keys = [d.get("key") for _, d in dashboards]
    errs += [f"dashboard key '{k}' is used twice" for k in set(keys) if keys.count(k) > 1]
    if errs:
        raise MBError("the specs in " + spec_dir + " have errors, nothing applied:\n  " + "\n  ".join(errs))

    db_id = database_id(mb, model["database"])
    meta = sync_and_wait(mb, db_id, model)
    apply_model(mb, db_id, model, meta)

    gids = groups_by_name(mb)
    parent, team_ids = apply_collections(mb, colls, gids)
    apply_data_perms(mb, db_id, colls, gids)

    found = managed_dashboards(mb, [parent, *team_ids.values()])
    bad = 0
    for f, d in dashboards:
        _, placed = apply_dashboard(mb, db_id, d, team_ids[d["collection"]], found.get(d["key"]))
        bad += check_cards(mb, d, placed, verbose)
    if bad:
        log(f"WARNING: {bad} card(s) failed or were empty (above)")


if __name__ == "__main__":
    try:
        main()
    except MBError as e:
        print(f"metabase: ERROR: {e}", file=sys.stderr)
        sys.exit(1)
