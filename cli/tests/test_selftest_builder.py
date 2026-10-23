"""scripts/selftest.py: the variants of the self-test app that the platform tests deploy. Run:
PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli" / "src"))
spec = importlib.util.spec_from_file_location("selftest", ROOT / "scripts" / "selftest.py")
selftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selftest)

from annex_cli import manifest as mf  # noqa: E402


def build(*args: str) -> Path:
    out = Path(tempfile.mkdtemp()) / "app"
    a = selftest.argparse.Namespace(out=str(out), name="annex-selftest", owner="sam", drop=None, job_only=False, broken_health=False,
                                    no_postgres=False, no_views=False, evil_label=False, big_file=None, views=None, egress=None, env=None, secret=None)
    p = selftest.argparse.ArgumentParser()
    for flag in ("--name", "--owner", "--drop", "--views", "--egress"):
        p.add_argument(flag, default=getattr(a, flag[2:]))
    for flag in ("--job-only", "--broken-health", "--no-postgres", "--no-views", "--evil-label"):
        p.add_argument(flag, action="store_true")
    p.add_argument("--big-file", type=int)
    p.add_argument("--env", action="append")
    p.add_argument("--secret", action="append")
    ns = p.parse_args(list(args))
    ns.out = str(out)
    selftest.build(ns)
    return out


class Variants(unittest.TestCase):
    def load(self, out: Path) -> mf.Manifest:
        return mf.load(out)

    def test_the_base_app_is_a_valid_manifest_that_uses_every_feature(self):
        m = self.load(build())
        self.assertEqual((m.name, m.owner, m.type), ("annex-selftest", "sam", "web+job"))
        self.assertTrue(m.postgres and m.files and m.notify_email)
        self.assertEqual(m.notify_slack, ["#annex-selftest"])
        self.assertEqual([w.name for w in m.webhooks], ["trello", "gh", "plain"])
        self.assertEqual(sorted(m.secrets), ["GH_SECRET", "PLAIN_SECRET", "TRELLO_SECRET"])
        self.assertEqual({j.name for j in m.schedule}, {"tick", "work", "notify"})
        self.assertEqual(m.views, ["venues", "machine_models"])

    def test_the_fixture_never_posts_to_a_business_channel(self):
        text = (ROOT / "tests" / "annex-selftest" / "annex.yaml").read_text()
        self.assertNotIn("finance", text)
        self.assertEqual(self.load(build()).notify_slack, ["#annex-selftest"])

    def test_the_evil_label_variant_adds_a_traefik_label_after_the_user_line(self):
        plain = (build() / "Dockerfile").read_text()
        evil = (build("--evil-label") / "Dockerfile").read_text()
        self.assertNotIn("traefik", plain)
        self.assertIn("USER 10001\nLABEL traefik.http.routers.evil.rule=Host(`evil.example`)\n", evil)

    def test_the_big_file_variant_adds_a_file_of_that_size_before_the_user_line(self):
        plain = (build() / "Dockerfile").read_text()
        big = (build("--big-file", "30") / "Dockerfile").read_text()
        self.assertNotIn("urandom", plain)
        self.assertIn("RUN head -c 31457280 /dev/urandom > /opt/big.bin\nUSER 10001\n", big)

    def test_drop_sections(self):
        m = self.load(build("--drop", "webhooks,notify"))
        self.assertEqual((m.webhooks, m.notify_email, m.notify_slack, m.secrets), ([], False, [], []))

    def test_a_job_only_variant_has_no_web_part_and_no_webhooks(self):
        m = self.load(build("--name", "annex-selftest-job", "--job-only", "--env", "SELFTEST_LOG=plain"))
        self.assertEqual((m.type, m.port, m.healthcheck, m.webhooks, m.secrets), ("job", None, None, [], []))
        self.assertEqual(m.env["SELFTEST_LOG"], "plain")

    def test_broken_health_name_owner_env_secret_postgres_views(self):
        m = self.load(build("--broken-health", "--name", "annex-selftest-x", "--owner", "kaldi-admin", "--env", "SELFTEST_MARKER=m 1",
                            "--env", "SELFTEST_GREETING=Hi", "--secret", "AUDIT_KEY", "--no-postgres", "--no-views", "--drop", "webhooks"))
        self.assertEqual((m.healthcheck, m.name, m.owner, m.postgres, m.views), ("/nope", "annex-selftest-x", "kaldi-admin", False, []))
        self.assertEqual((m.env["SELFTEST_MARKER"], m.env["SELFTEST_GREETING"], m.secrets), ("m 1", "Hi", ["AUDIT_KEY"]))

    def test_views_replace_the_fixtures_views(self):
        m = self.load(build("--views", "leaderboard,machine_floor"))
        self.assertEqual(m.views, ["leaderboard", "machine_floor"])

    def test_egress_declares_hosts_and_the_fixture_has_none(self):
        self.assertEqual(self.load(build()).egress, [])
        self.assertEqual(self.load(build("--egress", "one.one.one.one,example.com")).egress, ["one.one.one.one", "example.com"])

    def test_an_existing_env_entry_is_replaced_not_duplicated(self):
        out = build("--env", "SELFTEST_MARKER=once", "--env", "SELFTEST_MARKER=twice")
        self.assertEqual((out / "annex.yaml").read_text().count("SELFTEST_MARKER"), 1)
        self.assertEqual(self.load(out).env["SELFTEST_MARKER"], "twice")

    def test_dropping_an_unknown_section_says_so(self):
        with self.assertRaises(SystemExit):
            build("--drop", "nonsense")

    def test_the_copy_carries_the_runtime_and_no_markers(self):
        out = build()
        self.assertTrue((out / "annex_runtime.py").exists() and (out / "annex-errors.js").exists() and (out / "main.py").exists())
        self.assertFalse([ln for ln in (out / "annex.yaml").read_text().splitlines() if ln.startswith("# --- ")])


if __name__ == "__main__":
    unittest.main()
