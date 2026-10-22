"""Per-app networks (D88): the networks-sync flags, the rewrite of a pre-D88 compose file
and the egress check in the manifest."""
import tempfile
import unittest
from pathlib import Path

try:
    import yaml

    from annex_cli import manifest as mf
    from annex_cli import ops
    from annex_cli.remote import AnnexError
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class NetworkArgs(unittest.TestCase):
    """The generated compose file's networks are tested with the VM-side deploy (vm/tests/test_deploy_compose.py,
    D101); migrate-networks still calls networks-sync from here until V2."""

    def test_networks_sync_flags(self):
        self.assertEqual(ops._network_args("demo", web=True, postgres=True, views=True, egress=True),
                         ["networks-sync", "demo", "--web", "--postgres", "--views", "--egress"])
        self.assertEqual(ops._network_args("demo", web=False, postgres=False, views=False, egress=False),
                         ["networks-sync", "demo"])


OLD = {  # what compose_for wrote before D88, trimmed
    "name": "annex-app-vibe",
    "services": {"vibe": {
        "image": "annex-app/vibe:20261014", "container_name": "annex-app-vibe",
        "environment": {"ANNEX_APP_NAME": "vibe", "ANNEX_GATEWAY_URL": "http://gateway:3000", "PORT": "8080"},
        "labels": {"com.kaldi.annex": "true", "traefik.enable": "true",
                   "traefik.http.routers.app-vibe.rule": "Host(`vibe.annex.home.arpa`)"},
        "networks": ["edge", "appdata"]}},
    "networks": {"edge": {"external": True, "name": "annex-edge"}, "appdata": {"external": True, "name": "annex-appdata"}},
}


class Rewire(unittest.TestCase):
    def test_moves_a_web_app_with_postgres_and_views(self):
        doc, uses = ops.rewire_compose(OLD, "vibe", egress=False)
        self.assertEqual(uses, {"web": True, "postgres": True, "views": True, "egress": False})
        svc = doc["services"]["vibe"]
        self.assertEqual(svc["networks"], ["app"])
        self.assertEqual(doc["networks"], {"app": {"external": True, "name": "annex-app-vibe"}})
        self.assertEqual(svc["labels"]["traefik.docker.network"], "annex-app-vibe")
        self.assertEqual(svc["image"], "annex-app/vibe:20261014")  # everything else is left alone
        self.assertEqual(OLD["services"]["vibe"]["networks"], ["edge", "appdata"])  # the input isn't changed

    def test_egress_keeps_internet_access(self):
        doc, uses = ops.rewire_compose(OLD, "vibe", egress=True)
        self.assertEqual(doc["services"]["vibe"]["networks"], ["app", "egress"])
        self.assertTrue(uses["egress"])

    def test_an_edge_only_job(self):
        old = {"services": {"jj": {"labels": {"traefik.enable": "false"}, "environment": {}, "networks": ["edge"]}},
               "networks": {"edge": {"external": True, "name": "annex-edge"}}}
        doc, uses = ops.rewire_compose(old, "jj", egress=False)
        self.assertEqual(uses, {"web": False, "postgres": False, "views": False, "egress": False})
        self.assertNotIn("traefik.docker.network", doc["services"]["jj"]["labels"])

    def test_the_result_is_yaml_compose_can_read(self):
        doc, _ = ops.rewire_compose(OLD, "vibe", egress=False)
        self.assertEqual(yaml.safe_load(yaml.safe_dump(doc, sort_keys=False)), doc)

    def test_refuses_a_file_annex_didnt_write(self):
        with self.assertRaises(AnnexError):
            ops.rewire_compose({"services": {"a": {}, "b": {}}}, "vibe", egress=False)
        with self.assertRaises(AnnexError):
            ops.rewire_compose({"services": {"a": {"labels": ["traefik.enable=true"]}}}, "vibe", egress=False)


class EgressManifest(unittest.TestCase):
    def load(self, egress: str) -> mf.Manifest:
        d = Path(tempfile.mkdtemp())
        (d / "Dockerfile").write_text("FROM scratch\nUSER 10001\n")
        (d / "annex.yaml").write_text(f"name: demo\nowner: sam\ntype: web\nport: 8080\nhealthcheck: /healthz\n"
                                      f"access: internal\negress: {egress}\n")
        return mf.load(d)

    def test_hostnames_are_kept_lowercase(self):
        self.assertEqual(self.load("[api.Trello.com, '*.slack.com']").egress, ["api.trello.com", "*.slack.com"])
        self.assertEqual(self.load("[]").egress, [])

    def test_a_url_is_refused_with_what_egress_means(self):
        for bad in ("[https://api.trello.com]", "[api.trello.com/path]", "[localhost]", "[api.trello.com:443]", "yes"):
            with self.subTest(bad=bad), self.assertRaises(mf.ManifestError) as cm:
                self.load(bad)
            self.assertIn("no internet access at all", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
