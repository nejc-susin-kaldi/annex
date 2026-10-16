"""annex_vm.py's release rows (M2.2). Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
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

    def test_the_insert_is_idempotent(self):
        sql = vm.releases_sql([vm.release_row("x", LIVE)])
        self.assertIn("ON CONFLICT (app, deployed_at, release) DO NOTHING", sql)
        self.assertIn("jsonb_to_recordset", sql)


if __name__ == "__main__":
    unittest.main()
