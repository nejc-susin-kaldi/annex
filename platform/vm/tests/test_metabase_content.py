"""vm/metabase_content.py's pure parts and the specs in config/metabase (GA.3, D109).
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s platform/vm/tests"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metabase_content as mc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "config" / "metabase"
MODEL = mc.load_json(SPEC / "goat-arcade" / "model.json")
COLLS = mc.load_json(SPEC / "goat-arcade" / "collections.json")
PEOPLE = mc.load_json(SPEC / "people.json")
DASHES = {f: mc.load_json(SPEC / "goat-arcade" / "dashboards" / f)
          for f in sorted(os.listdir(SPEC / "goat-arcade" / "dashboards")) if f.endswith(".json")}
VIEWS = set(MODEL["views"])


class Specs(unittest.TestCase):
    def test_every_dashboard_is_valid(self):
        teams = [t["name"] for t in COLLS["teams"]]
        for f, d in DASHES.items():
            self.assertEqual(mc.validate_dashboard(d, VIEWS, teams, f), [], f)

    def test_five_dashboards_one_per_team(self):
        self.assertEqual(sorted(d["collection"] for d in DASHES.values()),
                         ["Community", "Finance", "Management", "Technical", "Venues"])
        keys = [d["key"] for d in DASHES.values()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_venue_filter_on_every_card_of_venue_performance(self):
        d = next(d for d in DASHES.values() if d["name"] == "Venue performance")
        self.assertTrue(d["parameters"][0]["every_card"])
        for c in d["cards"]:
            self.assertIn("{{venue}}", mc.sql_text(c["sql"]), c["key"])

    def test_the_model_lists_exactly_the_goat_arcade_views(self):
        # the views Metabase waits for and may read are the ones api_views_arcade.sql defines (a new view goes in both)
        import re
        sql = (ROOT / "db" / "product" / "clientlink" / "api_views_arcade.sql").read_text()
        defined = set(re.findall(r"CREATE OR REPLACE VIEW annex_api\.(\w+)", sql))
        self.assertEqual(VIEWS, defined)

    def test_groups_exist_and_sam_is_the_analyst(self):
        groups = set(PEOPLE["groups"]) | {"All Users"}
        for g in COLLS["query_builder"] + COLLS["native_sql"] + [t["group"] for t in COLLS["teams"]]:
            self.assertIn(g, groups)
        self.assertEqual(COLLS["native_sql"], ["Analysts"])
        analysts = [login for login, gs in PEOPLE["members"].items() if "Analysts" in gs]
        self.assertEqual(analysts, ["sam"])


class SqlGuard(unittest.TestCase):
    def test_views_ctes_and_subqueries_pass(self):
        sql = ("WITH l AS (SELECT max(day) AS d FROM annex_api.daily_plays) "
               "SELECT * FROM annex_api.venues v JOIN (SELECT 1 FROM annex_api.revenue_weekly) x ON true, l "
               "LEFT JOIN l AS l2 ON true WHERE extract(epoch FROM (now() - now())) > 0")
        self.assertEqual(mc.check_sql(sql, VIEWS), [])

    def test_raw_tables_and_other_schemas_fail(self):
        self.assertTrue(mc.check_sql("SELECT * FROM src_arcade.games", VIEWS))
        self.assertTrue(mc.check_sql("SELECT * FROM annex_api.not_a_view", VIEWS))
        self.assertTrue(mc.check_sql("SELECT * FROM venues", VIEWS))
        self.assertTrue(mc.check_sql("SELECT * FROM annex_gateway.request_log", VIEWS))
        self.assertTrue(mc.check_sql("SELECT * FROM pg_catalog.pg_roles", VIEWS))

    def test_spec_errors_say_what_to_fix(self):
        bad = {"key": "x", "name": "X", "collection": "Nope", "parameters": [{"slug": "venue", "name": "V",
                                                                           "every_card": True}],
               "cards": [{"key": "a", "name": "A", "display": "pie chart", "pos": [20, 0, 8, 4],
                          "sql": "SELECT {{city}} FROM annex_api.venues"}]}
        errs = "\n".join(mc.validate_dashboard(bad, VIEWS, ["Venues"], "x.json"))
        for part in ("collection 'Nope'", "display 'pie chart'", "within 24 columns", "{{city}}",
                     "[[AND <column> = {{venue}}]]"):
            self.assertIn(part, errs)


def field(fid, name, **kw):
    return {"id": fid, "name": name, "semantic_type": None, "fk_target_field_id": None, "settings": None,
            "display_name": name.replace("_", " ").title(), **kw}


META = {"tables": [
    {"id": 1, "schema": "annex_api", "name": "venues", "display_name": "Venues", "fields": [
        field(10, "venue_code"), field(11, "lat", semantic_type="type/Latitude"), field(12, "lon"),
        field(13, "opening_hours → mon")]},
    {"id": 2, "schema": "annex_api", "name": "revenue_weekly", "display_name": "Revenue Weekly", "fields": [
        field(20, "venue_code"), field(21, "amount_eur")]},
    {"id": 3, "schema": "annex_api", "name": "player_profiles", "display_name": "Player Profiles", "fields": [
        field(30, "nickname"), field(31, "home_venue_code")]},
    {"id": 4, "schema": "annex_api", "name": "client_summary", "display_name": "Client Summary", "fields": [
        field(40, "aum_eur")]},
]}


class Model(unittest.TestCase):
    def test_keys_currency_coordinates_names(self):
        ch = {name: c for _, name, c in mc.field_changes(MODEL, META)}
        self.assertEqual(ch["venues.venue_code"]["semantic_type"], "type/PK")
        self.assertEqual(ch["revenue_weekly.venue_code"], {"semantic_type": "type/FK", "fk_target_field_id": 10,
                                                           "display_name": "Venue"})
        self.assertEqual(ch["player_profiles.home_venue_code"]["fk_target_field_id"], 10)
        self.assertEqual(ch["player_profiles.nickname"]["semantic_type"], "type/PK")
        self.assertEqual(ch["revenue_weekly.amount_eur"]["semantic_type"], "type/Currency")
        self.assertEqual(ch["revenue_weekly.amount_eur"]["settings"], {"currency": "EUR"})
        self.assertEqual(ch["venues.lon"]["semantic_type"], "type/Longitude")
        self.assertNotIn("semantic_type", ch["venues.lat"])  # already right: only the name changes
        self.assertNotIn("venues.opening_hours → mon", ch)
        self.assertFalse(any(n.startswith("client_summary.") for n in ch))  # not a Goat Arcade view
        self.assertEqual({n for _, n, _ in mc.table_changes(MODEL, META)},
                         {"revenue_weekly", "player_profiles"})

    def test_applied_changes_leave_nothing_to_do(self):
        meta = json.loads(json.dumps(META))
        by_id = {f["id"]: f for t in meta["tables"] for f in t["fields"]}
        for fid, _, c in mc.field_changes(MODEL, meta):
            by_id[fid].update(c)
        for t in meta["tables"]:
            t["display_name"] = MODEL["tables"].get(t["name"], t["display_name"])
        self.assertEqual(mc.field_changes(MODEL, meta), [])
        self.assertEqual(mc.table_changes(MODEL, meta), [])


class Permissions(unittest.TestCase):
    GIDS = {"All Users": 1, "Administrators": 2, "Venues": 6, "Analysts": 10}

    def test_data_permissions_diff_then_nothing(self):
        graph = {"revision": 3, "groups": {
            "1": {"1": {"view-data": "unrestricted", "create-queries": "query-builder-and-native",
                        "download": {"schemas": "full"}}},
            "6": {"1": {"view-data": "unrestricted", "create-queries": "query-builder"}}}}
        diff = mc.data_perm_changes(graph, 1, self.GIDS, ["All Users", "Venues"], ["Analysts"])
        self.assertEqual(diff, {
            "1": {"1": {"view-data": "unrestricted", "create-queries": "query-builder", "download": {"schemas": "full"}}},
            "10": {"1": {"view-data": "unrestricted", "create-queries": "query-builder-and-native"}}})
        self.assertNotIn("2", diff)  # Administrators untouched
        for g, e in diff.items():
            graph["groups"].setdefault(g, {}).update(e)
        self.assertEqual(mc.data_perm_changes(graph, 1, self.GIDS, ["All Users", "Venues"], ["Analysts"]), {})

    def test_collection_permissions_diff(self):
        graph = {"revision": 2, "groups": {"1": {"root": "write", "12": "write", "14": "read"}}}
        wanted = {1: {12: "read", 14: "read"}, 6: {14: "write"}}
        self.assertEqual(mc.collection_perm_changes(graph, wanted), {"1": {"12": "read"}, "6": {"14": "write"}})
        graph["groups"]["1"]["12"] = "read"
        graph["groups"]["6"] = {"14": "write"}
        self.assertEqual(mc.collection_perm_changes(graph, wanted), {})


class Cards(unittest.TestCase):
    def test_viz_adds_titles_and_euros_and_keeps_the_spec(self):
        card = {"viz": {"graph.metrics": ["cash_eur"], "column_settings": {'["name","week"]': {"column_title": "Wk"}}},
                "titles": {"games_change_pct": "Games change (%)"}}
        viz = mc.card_viz(card, ["week", "cash_eur", "games_change_pct"])
        cs = viz["column_settings"]
        self.assertEqual(cs['["name","week"]'], {"column_title": "Wk"})
        self.assertEqual(cs['["name","cash_eur"]'], {"column_title": "Cash", "number_style": "currency",
                                                     "currency": "EUR", "currency_style": "symbol"})
        self.assertEqual(cs['["name","games_change_pct"]']["column_title"], "Games change (%)")
        self.assertEqual(card["viz"]["column_settings"], {'["name","week"]': {"column_title": "Wk"}})  # not mutated

    def test_native_query_in_both_formats(self):
        tags = {"venue": {"id": "x", "name": "venue", "display-name": "Venue", "type": "text", "required": False}}
        legacy = {"database": 1, "type": "native", "native": {"query": "SELECT 1", "template-tags": tags}}
        mbql5 = {"database": 1, "lib/type": "mbql/query",
                 "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT 1", "template-tags": tags}]}
        self.assertEqual(mc.native_of(legacy), ("SELECT 1", tags))
        self.assertEqual(mc.native_of(mbql5), ("SELECT 1", tags))
        mbql5["stages"][0]["template-tags"] = list(tags.values())  # what 0.64 returns
        self.assertEqual(mc.native_of(mbql5), ("SELECT 1", tags))

    def test_tag_and_parameter_ids_are_stable(self):
        params = [{"slug": "venue", "name": "Venue"}]
        a = mc.card_tags("ga.venue", "SELECT 1 [[AND v = {{venue}}]]", params)
        b = mc.card_tags("ga.venue", "SELECT 2 [[AND v = {{ venue }}]]", params)
        self.assertEqual(a, b)
        self.assertEqual(a["venue"]["type"], "text")
        self.assertEqual(mc.stable_id("ga.venue", "param", "venue", n=8), mc.stable_id("ga.venue", "param", "venue", n=8))

    def test_the_key_mark_is_found_again(self):
        text = mc.with_mark("What it shows.", "ga.glance.cash")
        self.assertEqual(mc.MARK_RE.search(text).group(1), "ga.glance.cash")
        self.assertTrue(text.startswith("What it shows.\n\n"))


if __name__ == "__main__":
    unittest.main()
