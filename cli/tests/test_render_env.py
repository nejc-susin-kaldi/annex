"""scripts/render.py: which settings file an environment reads, where it renders, and the local vs VM derivation (D99)."""
import importlib.util
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render", ROOT / "scripts" / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

VM = render.read_env(ROOT / ".env.example")
LOCAL = render.read_env(ROOT / ".env.local.example")


def derive(env, name="vm"):
    with unittest.mock.patch.dict(os.environ, {"HOME": "/Users/someone"}):
        return render.derive(dict(env), name)


class Selection(unittest.TestCase):
    def test_env_name(self):
        self.assertEqual(render.env_name({}), "vm")
        self.assertEqual(render.env_name({"ANNEX_ENV": ""}), "vm")
        self.assertEqual(render.env_name({"ANNEX_ENV": "local"}), "local")
        with self.assertRaises(SystemExit):
            render.env_name({"ANNEX_ENV": "prod"})

    def test_env_file_falls_back_to_the_example(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(render.env_file("local", root), root / ".env.local.example")
            (root / ".env.local").write_text("X=1\n")
            self.assertEqual(render.env_file("local", root), root / ".env.local")
            self.assertEqual(render.env_file("vm", root), root / ".env.example")

    def test_out_dir_per_env(self):
        self.assertEqual(render.out_dir("vm"), ROOT / ".build" / "vm" / "platform")
        self.assertEqual(render.out_dir("local"), ROOT / ".build" / "local" / "platform")

    def test_only_the_local_example_sets_a_target(self):
        self.assertEqual(LOCAL["ANNEX_TARGET"], "local")
        self.assertNotIn("ANNEX_TARGET", VM)


class VmDerivation(unittest.TestCase):
    def test_vm_binds_like_before(self):
        v = derive(VM)
        self.assertEqual((v["ANNEX_BIND_HTTP"], v["ANNEX_BIND_HTTPS"]), ("80", "443"))
        self.assertEqual((v["ANNEX_ENV"], v["ANNEX_TARGET"], v["ANNEX_DATA_DIR"]), ("vm", "ssh", "/srv/annex"))
        self.assertEqual(v["ANNEX_URL_HOME"], "https://home.annex.home.arpa")

    def test_vm_with_tunnel_port_keeps_443_binding(self):
        # the VM is reached through scripts/tunnel.sh on 8443, but Traefik still publishes 443 on the VM
        v = derive({**VM, "ANNEX_PUBLIC_PORT": "8443"})
        self.assertEqual(v["ANNEX_BIND_HTTPS"], "443")
        self.assertEqual(v["ANNEX_URL_HOME"], "https://home.annex.home.arpa:8443")

    def test_bad_bind_refused(self):
        for bad in ("abc", "127.0.0.1:", "0", "70000", "localhost:9443"):
            with self.assertRaises(SystemExit, msg=bad):
                derive({**VM, "ANNEX_BIND_HTTPS": bad})

    def test_bad_target_refused(self):
        with self.assertRaises(SystemExit):
            derive({**VM, "ANNEX_TARGET": "cloud"})


class LocalDerivation(unittest.TestCase):
    def test_local_example(self):
        v = derive(LOCAL, "local")
        self.assertEqual((v["ANNEX_ENV"], v["ANNEX_TARGET"]), ("local", "local"))
        self.assertEqual(v["ANNEX_DATA_DIR"], "/Users/someone/.annex/local")
        self.assertEqual((v["ANNEX_BIND_HTTP"], v["ANNEX_BIND_HTTPS"]), ("127.0.0.1:9080", "127.0.0.1:9443"))
        self.assertEqual(v["ANNEX_URL_HOME"], "https://home.annex-local.home.arpa:9443")
        self.assertEqual(v["ANNEX_COOKIE_DOMAIN"], "annex-local.home.arpa")
        self.assertEqual(v["COMPOSE_FILE"], "compose.yaml:compose.tls-local.yaml")

    def test_local_defaults_to_loopback(self):
        env = {k: v for k, v in LOCAL.items() if not k.startswith("ANNEX_BIND_")}
        v = derive(env, "local")
        self.assertEqual((v["ANNEX_BIND_HTTP"], v["ANNEX_BIND_HTTPS"]), ("127.0.0.1:9080", "127.0.0.1:9443"))

    def test_tilde_expands(self):
        v = derive({**LOCAL, "ANNEX_DATA_DIR": "~/x"}, "local")
        self.assertEqual(v["ANNEX_DATA_DIR"], "/Users/someone/x")

    def test_data_dir_inside_repo_refused(self):
        for bad in (str(ROOT), str(ROOT / ".build" / "data"), "relative/dir"):
            with self.assertRaises(SystemExit, msg=bad):
                derive({**LOCAL, "ANNEX_DATA_DIR": bad}, "local")

    def test_public_port_must_match_https_bind(self):
        with self.assertRaises(SystemExit):
            derive({**LOCAL, "ANNEX_PUBLIC_PORT": "8443"}, "local")


if __name__ == "__main__":
    unittest.main()
