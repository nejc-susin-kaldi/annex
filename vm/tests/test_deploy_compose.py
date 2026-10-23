"""The compose file a release runs with (generated on the VM since D101): per-app networks (D88), the JSON form
Compose reads, and the route probe's port. Moved from cli/tests/test_networks.py (compose_for); rewire_compose and
the networks-sync flags stay there. Stdlib unittest: python3 -m unittest discover -s vm/tests"""
import json
import unittest

from deploy_fakes import SETTINGS, Base, dep, manifest


class ComposeNetworks(Base):
    def compose(self, **kw) -> dict:
        return dep.compose_for(self.make_run(), manifest(**kw), "annex-app/demo:r1", 1)

    def test_a_web_app_is_on_its_own_network_only(self):
        c = self.compose(postgres=True, views=["v"])
        self.assertEqual(c["services"]["demo"]["networks"], ["app"])
        self.assertEqual(c["networks"], {"app": {"external": True, "name": "annex-app-demo"}})
        self.assertEqual(c["services"]["demo"]["labels"]["traefik.docker.network"], "annex-app-demo")

    def test_no_shared_network_in_any_variant(self):
        for kw in ({}, {"postgres": True}, {"views": ["v"]}, {"egress": ["api.trello.com"]},
                   {"type": "job", "port": None, "healthcheck": None}):
            names = {n["name"] for n in self.compose(**kw)["networks"].values()}
            self.assertFalse(names & {"annex-edge", "annex-appdata", "annex-platform", "annex-console"}, kw)

    def test_egress_adds_the_apps_own_egress_network(self):
        c = self.compose(egress=["api.trello.com"])
        self.assertEqual(c["services"]["demo"]["networks"], ["app", "egress"])
        self.assertEqual(c["networks"]["egress"], {"external": True, "name": "annex-egress-demo"})

    def test_a_job_has_no_traefik_network_label(self):
        labels = self.compose(type="job", port=None, healthcheck=None)["services"]["demo"]["labels"]
        self.assertNotIn("traefik.docker.network", labels)
        self.assertEqual(labels["traefik.enable"], "false")


class ComposeContent(Base):
    def test_urls_paths_and_the_project_name(self):
        c = dep.compose_for(self.make_run(), manifest(files=True, webhooks=[]), "annex-app/demo:r3", 3)
        svc = c["services"]["demo"]
        self.assertEqual(c["name"], "annex-app-demo")
        self.assertEqual(svc["environment"]["ANNEX_BASE_URL"], "https://demo.annex.home.arpa:8443")
        self.assertEqual(svc["env_file"], [f"{self.base}/secrets/apps/demo/runtime.env"])
        self.assertEqual(svc["volumes"], [f"{self.base}/apps/demo/data:/data"])
        self.assertEqual(svc["labels"]["traefik.http.routers.app-demo.rule"], "Host(`demo.annex.home.arpa`)")
        self.assertEqual(svc["labels"]["com.kaldi.annex.release"], "3")

    def test_jobs_become_ofelia_labels(self):
        m = manifest(type="web+job", schedule=[dep.mf.Job(name="nightly", cron="0 3 * * *", command=["python", "x.py"])])
        labels = dep.compose_for(self.make_run(), m, "i", 1)["services"]["demo"]["labels"]
        self.assertEqual(labels["ofelia.job-exec.demo-nightly.schedule"], "0 0 3 * * *")
        self.assertEqual(labels["ofelia.job-exec.demo-nightly.command"], "python x.py")
        self.assertEqual(labels["ofelia.enabled"], "true")

    def test_the_file_is_json_and_reads_back_whole(self):
        c = dep.compose_for(self.make_run(), manifest(postgres=True, views=["v"]), "annex-app/demo:r1", 1)
        self.assertEqual(json.loads(json.dumps(c, indent=2)), c)


class TileGroup(Base):
    """The Homepage group comes from the platform's config/teams.json, not from the manifest (D107)."""

    def group(self, **kw) -> str:
        c = dep.compose_for(self.make_run(), manifest(**kw), "annex-app/demo:r1", 1)
        return c["services"][kw.get("name", "demo")]["labels"]["homepage.group"]

    def write(self, text: str) -> None:
        cfg = self.base / "platform" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "teams.json").write_text(text)

    def test_no_file_means_tools(self):
        self.assertEqual(self.group(), "Tools")

    def test_an_app_in_the_file_gets_its_team(self):
        self.write('{"demo": "Venues", "other": "Finance"}')
        self.assertEqual(self.group(), "Venues")
        self.assertEqual(self.group(name="unlisted"), "Tools")

    def test_a_broken_file_or_a_bad_value_means_tools(self):
        for text in ("{not json", "[]", '{"demo": 3}', '{"demo": ""}', '{"demo": "a\\nb"}'):
            self.write(text)
            self.assertEqual(self.group(), "Tools", text)


class RoutePort(Base):
    def port(self, key, value, default):
        return dep.bound_port(self.make_run(settings={**SETTINGS, **({key: value} if value is not None else {})}),
                              key, default)

    def test_the_vm_binds_443_and_80_whatever_the_browsers_port_is(self):
        self.assertEqual(self.port("ANNEX_BIND_HTTPS", None, 443), 443)  # ANNEX_PUBLIC_PORT=8443 is the tunnel's
        self.assertEqual(self.port("ANNEX_BIND_HTTP", None, 80), 80)

    def test_local_mode_binds_loopback_ports(self):
        self.assertEqual(self.port("ANNEX_BIND_HTTPS", "127.0.0.1:9443", 443), 9443)
        self.assertEqual(self.port("ANNEX_BIND_HTTP", "127.0.0.1:9080", 80), 9080)
        self.assertEqual(self.port("ANNEX_BIND_HTTP", "8080", 80), 8080)
        self.assertEqual(self.port("ANNEX_BIND_HTTP", "", 80), 80)

    def test_the_route_probe_uses_the_bound_port(self):
        run = self.make_run(settings={**SETTINGS, "ANNEX_BIND_HTTPS": "127.0.0.1:9443"})
        dep.wait_route(run, manifest())
        [curl] = [c for c in self.docker.calls if c[0] == "curl"]
        self.assertIn("demo.annex.home.arpa:9443:127.0.0.1", curl)
        self.assertEqual(curl[-1], "https://demo.annex.home.arpa:9443/")


if __name__ == "__main__":
    unittest.main()
