"""annex_vm.py's release rows (M2.2). Stdlib unittest, runs on the Mac: python3 -m unittest discover -s platform/vm/tests"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402

LIVE = {"release": 3, "image": "annex-app/x:r3", "manifest": {"name": "x"}, "deployed_at": "2026-10-16T12:20:00+0200",
        "deployed_by": "sam@mac", "via": "cli", "git": {"sha": "abc", "dirty": False}, "snapshot": "pre-r3", "result": "live"}


class Rows(unittest.TestCase):
    def test_a_live_release(self):
        r = vm.release_row("x", LIVE)
        self.assertEqual((r["app"], r["release"], r["result"], r["note"], r["rollback_of"]), ("x", 3, "live", None, None))
        self.assertEqual(r["git"], {"sha": "abc", "dirty": False})
        self.assertEqual(r["manifest"], {"name": "x"})

    def test_a_failed_release_keeps_its_reason_and_where_it_went_back_to(self):
        r = vm.release_row("x", {**LIVE, "result": "failed", "error": "GET /healthz did not return 200", "rolled_back_to": 2})
        self.assertEqual(r["note"], "rolled back to r2: GET /healthz did not return 200")

    def test_a_rollback(self):
        r = vm.release_row("x", {**LIVE, "rollback": {"from": 5, "to": 3, "with_data": True}})
        self.assertEqual((r["note"], r["rollback_of"]), ("rollback from r5 to r3 with data", 5))

    def test_an_old_line_without_who_or_git_still_loads(self):
        r = vm.release_row("x", {"release": 1, "image": "i", "result": "live", "deployed_at": "2026-10-15T09:00:00+0200"})
        self.assertEqual((r["deployed_by"], r["git"], r["manifest"]), (None, None, {}))

    def test_a_line_without_the_essentials_is_skipped(self):
        self.assertIsNone(vm.release_row("x", {"release": 1}))


class Sql(unittest.TestCase):
    def test_json_is_dollar_quoted_with_a_tag_it_does_not_contain(self):
        nasty = {"app": "x", "note": "$annexjson$; DROP TABLE annex_ops.releases; --"}
        quoted = vm.dollar_quote(json.dumps([nasty]))
        tag = quoted[: quoted.index("$", 1) + 1]
        self.assertNotEqual(tag, "$annexjson$")
        self.assertTrue(quoted.startswith(tag) and quoted.endswith(tag))
        self.assertEqual(quoted.count(tag), 2)

    def test_the_cli_commit_travels_to_the_database_row(self):
        cli = {"commit": "a" * 40, "dirty": False}
        self.assertEqual(vm.release_row("x", {**LIVE, "cli": cli})["cli"], cli)
        self.assertIsNone(vm.release_row("x", LIVE)["cli"])  # a line from before the CLI reported its commit
        self.assertIn("cli jsonb", vm.RELEASE_COLUMNS)
        self.assertIn("r.cli", vm.releases_sql([vm.release_row("x", {**LIVE, "cli": cli})]))

    def test_the_migration_adds_the_column_and_is_new(self):
        sql = (Path(__file__).resolve().parents[2] / "db/appdata/migrations/005_releases_cli.sql").read_text()
        self.assertIn("ADD COLUMN IF NOT EXISTS cli jsonb", sql)

    def test_the_insert_is_idempotent(self):
        sql = vm.releases_sql([vm.release_row("x", LIVE)])
        self.assertIn("ON CONFLICT (app, deployed_at, release) DO NOTHING", sql)
        self.assertIn("jsonb_to_recordset", sql)


if __name__ == "__main__":
    unittest.main()


class Audit(unittest.TestCase):
    """The audit rows made from releases.jsonl lines (M2.5)."""

    def test_a_deploy(self):
        a = vm.release_audit("x", LIVE)
        self.assertEqual((a["action"], a["actor"], a["app"], a["ts"]), ("deploy", "sam@mac", "x", "2026-10-16T12:20:00+0200"))
        self.assertEqual(a["details"], {"release": 3, "via": "cli", "git": "abc"})
        self.assertEqual(a["ref"], "release:x:2026-10-16T12:20:00+0200:3")

    def test_a_failed_deploy_says_why_and_where_it_went_back_to(self):
        a = vm.release_audit("x", {**LIVE, "result": "failed", "error": "GET /healthz did not return 200", "rolled_back_to": 2})
        self.assertEqual(a["action"], "deploy.failed")
        self.assertEqual((a["details"]["error"], a["details"]["rolled_back_to"]), ("GET /healthz did not return 200", 2))

    def test_a_rollback_with_data_and_a_failed_one(self):
        a = vm.release_audit("x", {**LIVE, "via": "mcp", "rollback": {"from": 5, "to": 3, "with_data": True}})
        self.assertEqual((a["action"], a["details"]["from"], a["details"]["to"], a["details"]["with_data"], a["details"]["via"]), ("rollback", 5, 3, True, "mcp"))
        self.assertEqual(vm.release_audit("x", {**LIVE, "result": "failed", "rollback": {"from": 5, "to": 3, "with_data": False}})["action"], "rollback.failed")

    def test_a_line_without_the_essentials_has_no_audit_row(self):
        self.assertIsNone(vm.release_audit("x", {"release": 1}))

    def test_the_insert_is_idempotent_and_quotes_safely(self):
        sql = vm.audit_sql([vm.audit_row("secret.set", "sam@mac", "x", {"names": ["A", "$annexjson$; DROP"]})])
        self.assertIn("ON CONFLICT (ref) DO NOTHING", sql)
        self.assertNotIn("$annexjson$[", sql)

    def test_secret_values_have_no_place_in_the_row(self):
        row = vm.audit_row("secret.set", "sam@mac", "x", {"names": ["API_KEY"]})
        self.assertEqual(row["details"], {"names": ["API_KEY"]})
