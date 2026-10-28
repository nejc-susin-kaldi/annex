"""annex_vm's database side (C6, D96): the commands that shell out to Docker and psql, run against a fake `run`.

The fake records every command (and the stdin it was given), so the tests assert the SQL that reaches psql, that no
password is on a command line, and the order of the Docker calls. Stdlib unittest, runs on the Mac:
python3 -m unittest discover -s platform/vm/tests"""
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402

IMAGE = "postgres:16.4-alpine"


class FakeDocker:
    """Stands in for annex_vm.run. `calls` holds (argv, stdin text); `psql` maps a substring of the SQL to its output."""

    def __init__(self, psql=None, fail=()):
        self.calls: list[tuple[list[str], str | None]] = []
        self.psql = {"information_schema.views": "orders\ncustomers", "datname = $annexjson$annex_ops$annexjson$": "1",
                     **(psql or {})}
        self.fail = fail  # substrings of the joined argv that make the call return 1

    def __call__(self, argv, input=None, capture_output=False, text=False, check=False, stdin=None, stdout=None,
                 stderr=None):
        self.calls.append((list(argv), input))
        joined = " ".join(argv)
        rc = 1 if any(f in joined for f in self.fail) else 0
        out = ""
        if argv[:2] == ["docker", "inspect"]:
            out = IMAGE if "Config.Image" in joined and "|" not in joined else ""
        elif "psql" in argv:
            out = next((v for k, v in self.psql.items() if k in (input or "")), "")
        elif "pg_dump" in argv and hasattr(stdout, "write"):
            stdout.write(b"PGDMP-fake")
        elif argv[:2] == ["docker", "run"] and "du" in argv:
            out = "12\t/d\n"
        elif argv[:2] == ["docker", "run"] and "tar" in argv and hasattr(stdout, "write"):
            stdout.write(b"tar-fake")
        if rc and check:
            raise subprocess.CalledProcessError(rc, argv)
        res = subprocess.CompletedProcess(argv, rc, stdout=out if text or stdout is None else out.encode(),
                                          stderr="boom" if rc and text else b"boom" if rc else ("" if text else b""))
        return res

    def sqls(self, container=None):
        return [sql for argv, sql in self.calls if "psql" in argv and (container is None or container in argv)]

    def commands(self, word):
        return [argv for argv, _ in self.calls if word in argv]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for name, value in (("DATA", d), ("SECRETS", d / "secrets"), ("BACKUPS", d / "backups"), ("SNAPSHOTS", d / "snapshots")):
            p = unittest.mock.patch.object(vm, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.d = d
        (d / "secrets").mkdir(parents=True)
        vm.write_kv(d / "secrets" / "master.env", {"GATEWAY_JWT_SECRET": "test-secret-" + "x" * 32})
        self.fake = FakeDocker()
        p = unittest.mock.patch.object(vm, "run", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def call(self, fn, args, stdin=""):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
            fn(argparse.Namespace(**args))
        return json.loads(buf.getvalue())


class Quoting(unittest.TestCase):
    def test_qlit_is_dollar_quoted(self):
        self.assertEqual(vm.qlit("abc"), "$annexjson$abc$annexjson$")

    def test_qlit_survives_quotes_and_dollars(self):
        for text in ["it's", "a$$b", "$annexjson$", "x$annexjson", "$annexjson$ and $annexjson1$", "end$", "", "a\\b"]:
            lit = vm.qlit(text)
            tag = lit[:lit.index("$", 1) + 1]
            self.assertTrue(lit.startswith(tag) and lit.endswith(tag), lit)
            body = lit[len(tag):-len(tag)]
            self.assertEqual(body, text)
            self.assertNotIn(tag, body)
            self.assertNotIn(tag, (body + tag)[:-1], "the closing tag must not be completed early")

    def test_dollar_quote_tail_that_completes_the_tag(self):
        lit = vm.dollar_quote("x$annexjson")  # the text's tail plus the closing tag would read "$annexjson$" early
        self.assertEqual(lit, "$annexjson1$x$annexjson$annexjson1$")

    def test_qident(self):
        self.assertEqual(vm.qident("app_x"), '"app_x"')
        self.assertEqual(vm.qident('a"b; DROP'), '"a""b; DROP"')


class Provision(Base):
    ARGS = dict(app="x-y", views="orders", secrets="API", postgres=True, notify_email=True, slack="#a-b,#c",
                hooks="", hook_port=8000, files=False)

    def provision(self, **over):
        sdir = vm.app_secrets_dir("x-y")
        (sdir / "secrets.env").write_text("API='k'\n")
        return self.call(vm.cmd_provision, {**self.ARGS, **over})

    def chowns(self):
        return [a for a in self.fake.commands("chown") if "-R" in a]

    def test_files_chowned_when_owner_differs(self):
        # a test runs as the Mac or VM user, never uid 10001: the first provision chowns /data
        self.assertTrue(self.provision(files=True)["ok"])
        self.assertTrue((self.d / "apps" / "x-y" / "data").is_dir())
        self.assertEqual(len(self.chowns()), 1)
        self.assertIn(f"{vm.APP_UID}:{vm.APP_UID}", self.chowns()[0])

    def test_files_not_chowned_when_owned_already(self):
        with unittest.mock.patch.object(vm, "owner", return_value=vm.APP_UID):
            self.assertTrue(self.provision(files=True)["ok"])
        self.assertEqual(self.chowns(), [])

    def test_files_owner_unreadable_chowns_each_time(self):
        # Docker Desktop bind mount (D99): the Mac side can't see the chown, or the owner can't be read: chown again,
        # every deploy, and never fail on it
        with unittest.mock.patch.object(vm, "owner", return_value=None):
            self.assertTrue(self.provision(files=True)["ok"])
            self.assertTrue(self.provision(files=True)["ok"])
        self.assertEqual(len(self.chowns()), 2)

    def test_owner_of_a_missing_path_is_none(self):
        self.assertIsNone(vm.owner(self.d / "nope"))
        self.assertIsInstance(vm.owner(self.d), int)

    def test_sql_for_a_full_app(self):
        res = self.provision()
        self.assertTrue(res["ok"])
        self.assertEqual(res["injected"], ["ANNEX_GATEWAY_TOKEN", "ANNEX_OPS_TOKEN", "API", "DATABASE_URL"])
        product = [s for s in self.fake.sqls(vm.PRODUCT_DB) if "annex_api" in s and "GRANT" in s][0]
        self.assertIn("$annexjson$app_x_y$annexjson$", product)
        self.assertIn('GRANT SELECT ON annex_api."orders" TO "app_x_y";', product)
        self.assertIn('GRANT "app_x_y" TO authenticator;', product)
        self.assertIn("format('CREATE ROLE %I NOLOGIN'", product)
        self.assertNotIn("DO $$", product)
        appdata = [s for s in self.fake.sqls(vm.APPDATA_DB) if "ALTER ROLE" in s][0]
        pw = (self.d / "secrets" / "apps" / "x-y" / "db_password").read_text().strip()
        self.assertIn(f'ALTER ROLE "app_x_y" WITH LOGIN PASSWORD $annexjson${pw}$annexjson$;', appdata)
        self.assertIn("format('CREATE DATABASE %I OWNER %I'", appdata)
        self.assertIn('GRANT CONNECT ON DATABASE "app_x_y" TO "app_x_y";', appdata)
        self.assertIn("\\gexec", appdata)
        grant = [s for s in self.fake.sqls(vm.APPDATA_DB) if "app_grants" in s][0]
        self.assertIn("VALUES ($annexjson$x-y$annexjson$, $annexjson$", grant)
        self.assertIn("ARRAY[$annexjson$#a-b$annexjson$,$annexjson$#c$annexjson$]::text[]", grant)
        self.assertIn("true, ", grant)

    def test_every_psql_runs_with_stdin_and_no_secret_on_a_command_line(self):
        self.provision()
        pw = (self.d / "secrets" / "apps" / "x-y" / "db_password").read_text().strip()
        token = (self.d / "secrets" / "apps" / "x-y" / "ops_token").read_text().strip()
        for argv, stdin in self.fake.calls:
            joined = " ".join(argv)
            self.assertNotIn(pw, joined)
            self.assertNotIn(token, joined)
            self.assertNotIn("PASSWORD", joined)
            if "psql" in argv:
                self.assertEqual(argv[:4], ["docker", "exec", "-i", argv[3]])
                self.assertIsNotNone(stdin)
        self.assertIn(pw, "".join(s for _, s in self.fake.calls if s))

    def test_gateway_token_changes_every_provision_and_is_not_printed(self):
        first = self.provision()
        t1 = (self.d / "secrets" / "apps" / "x-y" / "gateway_token").read_text()
        with unittest.mock.patch.object(vm.time, "time", return_value=2_000_000_000):
            self.provision()
        t2 = (self.d / "secrets" / "apps" / "x-y" / "gateway_token").read_text()
        self.assertNotEqual(t1, t2)
        self.assertNotIn(t1, json.dumps(first))

    def test_unknown_view_refused_before_any_grant(self):
        with self.assertRaises(vm.Fail) as e:
            self.provision(views="nope")
        self.assertIn("allowlist", str(e.exception))
        self.assertFalse([s for s in self.fake.sqls() if "GRANT SELECT" in s])

    def test_no_views_drops_the_role(self):
        self.provision(views="", slack="", notify_email=False, postgres=False)
        drop = [s for s in self.fake.sqls(vm.PRODUCT_DB) if "DROP OWNED" in s][0]
        self.assertIn("format('DROP OWNED BY %I', rolname) FROM pg_roles WHERE rolname = $annexjson$app_x_y$annexjson$", drop)
        self.assertIn("format('DROP ROLE %I', rolname)", drop)

    def test_missing_secret_refused(self):
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_provision, {**self.ARGS, "secrets": "OTHER"})
        self.assertEqual(self.fake.calls, [])

    def test_values_with_quotes_stay_inside_their_literal(self):
        nasty = "o'brien$$; DROP TABLE x; --"
        with unittest.mock.patch.object(vm, "role_name", return_value=nasty):
            vm.ensure_database("x-y")
            vm.grant_views("x-y", ["orders"])
        sql = self.fake.sqls(vm.APPDATA_DB)[0]
        self.assertIn(vm.qlit(nasty), sql)
        self.assertIn(vm.qident(nasty), sql)
        # outside literals and identifiers there is no trace of the value
        stripped = sql.replace(vm.qlit(nasty), "L").replace(vm.qident(nasty), "I")
        self.assertNotIn("DROP TABLE", stripped)
        self.assertNotIn("o'brien", stripped)

    def test_deprovision_sql(self):
        (self.d / "deploy" / "x-y").mkdir(parents=True)
        res = self.call(vm.cmd_deprovision, {"app": "x-y", "keep_data": False, "actor": "kim@mac"})
        self.assertIn("database", res["removed"])
        sqls = " ".join(s for s in self.fake.sqls())
        self.assertIn("DELETE FROM annex_ops.app_grants WHERE app = $annexjson$x-y$annexjson$;", sqls)
        self.assertIn("DELETE FROM annex_ops.error_groups WHERE app = $annexjson$x-y$annexjson$;", sqls)
        self.assertIn('DROP DATABASE IF EXISTS "app_x_y" WITH (FORCE);\nDROP ROLE IF EXISTS "app_x_y";', sqls)

    def test_psql_failure_is_a_fail(self):
        self.fake.fail = ("psql",)
        with self.assertRaises(vm.Fail):
            vm.psql(vm.APPDATA_DB, "postgres", "SELECT 1;")


class Snapshot(Base):
    def test_snapshot_dumps_the_database_and_archives_data(self):
        self.fake.psql["datname = $annexjson$app_x$annexjson$"] = "1"
        (self.d / "apps" / "x" / "data").mkdir(parents=True)
        res = self.call(vm.cmd_snapshot, {"app": "x", "release": 5, "from_release": 4})
        snap = res["snapshot"]
        self.assertEqual((snap["name"], snap["db"], snap["files"], snap["from_release"]), ("pre-r5", True, True, 4))
        target = self.d / "snapshots" / "x" / "pre-r5"
        self.assertEqual((target / "db.dump").read_bytes(), b"PGDMP-fake")
        self.assertTrue((target / "data.tar.gz").exists())
        self.assertEqual(self.fake.commands("pg_dump")[0], ["docker", "exec", vm.APPDATA_DB, "pg_dump", "-U", "postgres", "-Fc", "app_x"])
        helper = self.fake.commands("du")[0]
        self.assertIn("none", helper)
        self.assertIn(IMAGE, helper)

    def test_snapshot_of_an_app_with_nothing_is_none(self):
        res = self.call(vm.cmd_snapshot, {"app": "x", "release": 1, "from_release": None})
        self.assertIsNone(res["snapshot"])
        self.assertFalse((self.d / "snapshots" / "x" / "pre-r1").exists())

    def test_big_data_dir_is_skipped(self):
        (self.d / "apps" / "x" / "data").mkdir(parents=True)
        with unittest.mock.patch.object(vm, "SNAPSHOT_MAX_FILES_KB", 5):
            res = self.call(vm.cmd_snapshot, {"app": "x", "release": 2, "from_release": 1})
        self.assertTrue(res["snapshot"]["files"].startswith("skipped"))
        self.assertFalse(self.fake.commands("tar"))

    def test_pg_dump_failure_removes_the_half_snapshot(self):
        self.fake.psql["datname = $annexjson$app_x$annexjson$"] = "1"
        self.fake.fail = ("pg_dump",)
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_snapshot, {"app": "x", "release": 3, "from_release": 2})
        self.assertFalse((self.d / "snapshots" / "x" / "pre-r3").exists())

    def test_restore_snapshot_runs_stop_restore_in_order(self):
        self.fake.psql["datname = $annexjson$app_x$annexjson$"] = "1"
        (self.d / "apps" / "x" / "data").mkdir(parents=True)
        self.call(vm.cmd_snapshot, {"app": "x", "release": 5, "from_release": 4})
        self.fake.calls.clear()
        res = self.call(vm.cmd_restore_snapshot, {"app": "x", "from_release": 4})
        self.assertEqual((res["snapshot"], res["database"], res["files"]), ("pre-r5", True, True))
        order = [" ".join(argv[:5]) for argv, _ in self.fake.calls]
        self.assertLess(order.index("docker stop annex-app-x"), order.index("docker exec -i annex-appdata-db-1 psql"))
        sqls = self.fake.sqls(vm.APPDATA_DB)
        self.assertIn('DROP DATABASE IF EXISTS "app_x" WITH (FORCE);', sqls[0])
        self.assertTrue(any("ALTER ROLE" in s for s in sqls))
        restore = self.fake.commands("pg_restore")[0]
        self.assertEqual(restore[-5:], ["-U", "postgres", "-d", "app_x", "--exit-on-error"])
        self.assertFalse(self.fake.commands("start"), "the CLI starts the release it rolls back to, not this command")

    def test_restore_snapshot_without_a_match_refuses(self):
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_restore_snapshot, {"app": "x", "from_release": 9})
        self.assertEqual(self.fake.calls, [])


class Restore(Base):
    def archive(self, with_dump=True):
        (self.d / "backups").mkdir(parents=True)
        path = self.d / "backups" / "annex-1.tar.gz"
        src = self.d / "src"
        (src / "backup" / "apps" / "x" / "data").mkdir(parents=True)
        (src / "backup" / "apps" / "x" / "data" / "f.txt").write_text("hi")
        (src / "backup" / "dumps" / "current").mkdir(parents=True)
        (src / "backup" / "dumps" / "current" / "app_x.dump").write_bytes(b"PGDMP")
        with tarfile.open(path, "w:gz") as t:
            t.add(src / "backup" / "apps", arcname="backup/apps")
            if with_dump:
                t.add(src / "backup" / "dumps", arcname="backup/dumps")
        return path

    def deployed(self, postgres=True):
        (self.d / "deploy" / "x").mkdir(parents=True)
        (self.d / "deploy" / "x" / "deploy.json").write_text(json.dumps({"postgres": postgres}))

    def test_restore_order_and_sql(self):
        self.archive()
        self.deployed()
        res = self.call(vm.cmd_restore, {"app": "x", "archive": None, "actor": "kim@mac"})
        self.assertEqual((res["database"], res["files"]), (True, 1))
        names = [" ".join(argv[:3]) for argv, _ in self.fake.calls]
        stop, start = names.index("docker stop annex-app-x"), names.index("docker start annex-app-x")
        self.assertLess(stop, start)
        self.assertTrue(any("pg_restore" in c for c in names[stop:start]) or self.fake.commands("pg_restore"))
        self.assertIn('DROP DATABASE IF EXISTS "app_x" WITH (FORCE);', self.fake.sqls(vm.APPDATA_DB)[0])
        maint = [argv for argv, _ in self.fake.calls if "annex_ops.ctl" in argv]
        self.assertEqual(len(maint), 2)  # hold, then release
        self.assertIn("--minutes", maint[0])
        self.assertIn("--end", maint[1])
        self.assertFalse((self.d / "restore" / "x").exists())

    def test_restore_starts_the_app_even_when_pg_restore_fails(self):
        self.archive()
        self.deployed()
        self.fake.fail = ("pg_restore",)
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_restore, {"app": "x", "archive": None, "actor": None})
        self.assertTrue(self.fake.commands("start"))

    def test_restore_refuses_an_archive_without_the_dump(self):
        self.archive(with_dump=False)
        self.deployed()
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_restore, {"app": "x", "archive": None, "actor": None})
        self.assertFalse(self.fake.commands("stop"))

    def test_restore_of_an_undeployed_app_refuses(self):
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_restore, {"app": "x", "archive": None, "actor": None})


class ReleaseRecord(Base):
    REL = {"release": 3, "image": "annex-app/x:r3", "result": "live", "deployed_at": "2026-10-22T10:00:00+0200",
           "deployed_by": "kim@mac", "via": "cli", "manifest": {"name": "x"}}

    def test_release_record_sends_one_call_with_release_and_audit(self):
        res = self.call(vm.cmd_release_record, {"app": "x"}, json.dumps(self.REL))
        self.assertEqual(res, {"ok": True, "recorded": 1})
        sqls = self.fake.sqls(vm.APPDATA_DB)
        self.assertEqual(len(sqls), 2)  # ops_db_exists, then the insert
        sql = sqls[1]
        self.assertIn("INSERT INTO annex_ops.releases", sql)
        self.assertIn("INSERT INTO annex_ops.audit_events", sql)
        self.assertIn("ON CONFLICT (app, deployed_at, release) DO NOTHING;", sql)
        self.assertEqual(self.fake.calls[-1][0][-2:], ["-d", vm.OPS_DB])

    def test_values_with_quotes_and_dollars_stay_inside_the_literal(self):
        rel = {**self.REL, "error": "it's $annexjson$ broken $$", "deployed_by": "o'brien"}
        self.call(vm.cmd_release_record, {"app": "x"}, json.dumps(rel))
        sql = self.fake.sqls(vm.APPDATA_DB)[1]
        payload = json.dumps([vm.release_row("x", rel)])
        self.assertIn(vm.dollar_quote(payload) + "::jsonb", sql)
        self.assertNotIn("$annexjson$ broken", sql.replace(vm.dollar_quote(payload), ""))

    def test_bad_line_refused_before_psql(self):
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_release_record, {"app": "x"}, json.dumps({"release": 1}))
        self.assertEqual(self.fake.calls, [])

    def test_no_ops_database_is_a_fail(self):
        self.fake.psql["datname = $annexjson$annex_ops$annexjson$"] = ""
        with self.assertRaises(vm.Fail):
            self.call(vm.cmd_release_record, {"app": "x"}, json.dumps(self.REL))
        self.assertEqual(len(self.fake.sqls()), 1)


class DeployEnd(Base):
    LINE = {"manifest": {"name": "x"}, "deployed_at": "2026-10-22T11:00:00+0200", "deployed_by": "kim@mac", "via": "cli"}

    def ended(self):
        cur = {"pid": 1, "actor": "kim@mac", "step": "begin"}
        res = vm.begin(self.d, "x", dict(self.LINE), {"name": "x"}, cur, inspect=lambda app, m: None)
        n = res["release"]
        return vm.end(self.d, "x", n, "live", {**self.LINE, "release": n, "image": f"annex-app/x:r{n}"}), n

    def test_the_final_line_is_copied_to_the_database(self):
        final, n = self.ended()
        self.assertIsNone(vm.record_release_db("x", final))
        sql = self.fake.sqls(vm.APPDATA_DB)[-1]
        self.assertIn("INSERT INTO annex_ops.releases", sql)
        self.assertIn(f'"release": {n}', sql)

    def test_a_psql_failure_is_a_warning_not_an_error(self):
        final, _ = self.ended()
        self.fake.fail = ("psql",)
        self.assertIn("next scripts/push.sh catches up", vm.record_release_db("x", final))


if __name__ == "__main__":
    unittest.main()


class LoopbackTarget(unittest.TestCase):
    def test_vm_and_local_binds(self):
        self.assertEqual(vm.loopback_target("443", "443"), "127.0.0.1:443")
        self.assertEqual(vm.loopback_target("127.0.0.1:9443", "443"), "127.0.0.1:9443")
        self.assertEqual(vm.loopback_target("", "80"), "127.0.0.1:80")
        self.assertEqual(vm.loopback_target(None, "443"), "127.0.0.1:443")
