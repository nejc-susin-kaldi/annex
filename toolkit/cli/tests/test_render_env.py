"""scripts/render.py: which settings file an environment reads, where it renders, and the local vs VM derivation (D99);
another instance's lane and its settings (D118)."""
import importlib.util
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # the repo
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
        self.assertEqual(render.env_name({"ANNEX_ENV": "kaldi"}), "kaldi")
        for bad in ("Kaldi", "../x", "1st", "a b"):
            with self.assertRaises(SystemExit):
                render.env_name({"ANNEX_ENV": bad})

    def test_an_instance_reads_its_own_file_and_never_an_example(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env.example").write_text("X=1\n")
            with self.assertRaises(SystemExit):
                render.env_file("kaldi", root)
            (root / ".env.kaldi").write_text("X=2\n")
            self.assertEqual(render.env_file("kaldi", root), root / ".env.kaldi")
            self.assertEqual(render.out_dir("kaldi", root), root / ".build" / "kaldi" / "platform")

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


# an instance for real use on EC2 (D118): the settings docs/new-instance.md gives for it
KALDI = {**VM, "ANNEX_HOST_PATTERN": "{name}.annex.kaldi.si", "ANNEX_TLS_MODE": "acme",
         "ANNEX_ACME_EMAIL": "ops@kaldi.si", "ANNEX_DEMO": "off", "ANNEX_ADMIN_LOGIN": "nejc",
         "ANNEX_ADMIN_NAME": "Nejc Sušin", "ANNEX_ADMIN_EMAIL": "nejc@kaldi.si", "ANNEX_MAIL_RELAY": "on",
         "ANNEX_AUTH_FACTORS": "two"}


class InstanceSettings(unittest.TestCase):
    def test_defaults_are_the_prototype(self):
        v = derive(VM)
        self.assertEqual((v["ANNEX_DEMO"], v["ANNEX_MAIL_RELAY"], v["ANNEX_AUTH_POLICY"], v["ANNEX_TOTP_ISSUER"]),
                         ("on", "off", "one_factor", "Goat Arcade"))
        self.assertEqual(v["COMPOSE_FILE"], "compose.yaml:compose.tls-local.yaml")
        self.assertEqual(derive({**VM, "ANNEX_TLS_MODE": "upstream", "ANNEX_UPSTREAM_IP": "192.168.1.2"})["COMPOSE_FILE"],
                         "compose.yaml")

    def test_kaldi(self):
        v = derive(KALDI, "kaldi")
        self.assertEqual(v["COMPOSE_FILE"], "compose.yaml:compose.tls-acme.yaml:compose.mail-relay.yaml")
        self.assertEqual((v["ANNEX_CERT_DOMAIN"], v["ANNEX_URL_HOME"], v["ANNEX_FRONTDOOR_URL"]),
                         ("annex.kaldi.si", "https://home.annex.kaldi.si", "https://home.annex.kaldi.si/"))
        self.assertEqual(v["ANNEX_ACME_CA"], "https://acme-v02.api.letsencrypt.org/directory")
        self.assertEqual((v["ANNEX_AUTH_POLICY"], v["ANNEX_TOTP_ISSUER"], v["ANNEX_ADMIN_LOGIN"]),
                         ("two_factor", "Annex", "nejc"))
        self.assertIn("staging", derive({**KALDI, "ANNEX_ACME_STAGING": "true"})["ANNEX_ACME_CA"])

    def test_refusals(self):
        for change in ({"ANNEX_ACME_EMAIL": ""}, {"ANNEX_ACME_STAGING": "yes"}, {"ANNEX_DEMO": "no"},
                       {"ANNEX_ADMIN_LOGIN": ""}, {"ANNEX_ADMIN_LOGIN": "Nejc"}, {"ANNEX_ADMIN_EMAIL": "nejc"},
                       {"ANNEX_ADMIN_NAME": 'a "b"'}, {"ANNEX_MAIL_RELAY": "yes"}, {"ANNEX_AUTH_FACTORS": "2"},
                       # two factors need mail that leaves the server: the second factor is registered by an emailed code
                       {"ANNEX_MAIL_RELAY": "off"}, {"ANNEX_TLS_MODE": "letsencrypt"}):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                derive({**KALDI, **change})

    def test_demo_on_needs_no_admin(self):
        v = derive({**VM, "ANNEX_DEMO": "on"})
        self.assertEqual(v["ANNEX_ADMIN_LOGIN"], "")


class EmployeeAccess(unittest.TestCase):
    """ANNEX_ACCESS=headscale (D120): Annex's own tailnet, Headscale and webhooks on a second, public entrypoint."""

    def test_open_by_default(self):
        v = derive(VM)
        self.assertEqual((v["ANNEX_ACCESS"], v["ANNEX_BIND_PUBLIC"]), ("open", "8443"))
        self.assertNotIn("headscale", v["COMPOSE_FILE"])
        self.assertEqual(v["ANNEX_URL_HOOKS"], "https://hooks.annex.home.arpa" + v["ANNEX_PORT_SUFFIX"])
        self.assertEqual(derive(LOCAL, "local")["ANNEX_BIND_PUBLIC"], "127.0.0.1:9444")

    def test_headscale(self):
        v = derive({**KALDI, "ANNEX_ACCESS": "headscale"}, "kaldi")
        self.assertEqual(v["COMPOSE_FILE"],
                         "compose.yaml:compose.tls-acme.yaml:compose.mail-relay.yaml:compose.access-headscale.yaml")
        # Tailscale clients and webhook senders use the public entrypoint; everything else stays on 443
        self.assertEqual((v["ANNEX_URL_VPN"], v["ANNEX_URL_HOOKS"], v["ANNEX_URL_HOME"]),
                         ("https://vpn.annex.kaldi.si:8443", "https://hooks.annex.kaldi.si:8443", "https://home.annex.kaldi.si"))
        v = derive({**KALDI, "ANNEX_ACCESS": "headscale", "ANNEX_BIND_PUBLIC": "443", "ANNEX_BIND_HTTPS": "10.0.0.5:4443"}, "kaldi")
        self.assertEqual(v["ANNEX_URL_VPN"], "https://vpn.annex.kaldi.si")

    def test_oidc(self):
        v = derive({**KALDI, "ANNEX_ACCESS": "headscale", "ANNEX_ACCESS_OIDC_ISSUER": "https://login.microsoftonline.com/x/v2.0",
                    "ANNEX_ACCESS_OIDC_CLIENT_ID": "abc", "ANNEX_ACCESS_OIDC_DOMAINS": "kaldi.si example.com"}, "kaldi")
        self.assertTrue(v["COMPOSE_FILE"].endswith(":compose.access-headscale.yaml:compose.access-oidc.yaml"))
        self.assertEqual(v["ANNEX_ACCESS_OIDC_DOMAINS"], "kaldi.si example.com")

    def test_refusals(self):
        hs = {**KALDI, "ANNEX_ACCESS": "headscale"}
        oidc = {"ANNEX_ACCESS_OIDC_ISSUER": "https://idp.example", "ANNEX_ACCESS_OIDC_CLIENT_ID": "abc",
                "ANNEX_ACCESS_OIDC_DOMAINS": "kaldi.si"}
        for env in ({**KALDI, "ANNEX_ACCESS": "tailscale"},
                    # in upstream mode Traefik has no certificate for the public entrypoint
                    {**VM, "ANNEX_ACCESS": "headscale", "ANNEX_TLS_MODE": "upstream", "ANNEX_UPSTREAM_IP": "192.168.1.2"},
                    {**hs, "ANNEX_BIND_PUBLIC": "443"},             # the apps' port
                    {**hs, "ANNEX_BIND_PUBLIC": "x"},
                    {**KALDI, **oidc},                              # OIDC is the tailnet's sign-in
                    {**hs, "ANNEX_ACCESS_OIDC_ISSUER": "https://idp.example"},
                    {**hs, **oidc, "ANNEX_ACCESS_OIDC_ISSUER": "http://idp.example"},
                    {**hs, **oidc, "ANNEX_ACCESS_OIDC_DOMAINS": "kaldi.si,example.com"},
                    {**hs, **oidc, "ANNEX_ACCESS_OIDC_DOMAINS": ""}):  # whoever the provider signs in
            with self.subTest(env={k: v for k, v in env.items() if k.startswith("ANNEX_ACCESS") or k == "ANNEX_BIND_PUBLIC"}), \
                    self.assertRaises(SystemExit):
                derive(env, "kaldi")
