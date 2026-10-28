"""Per-app networks (D88): who joins an app's network, and the subnet picker. Stdlib unittest, runs on the Mac:
python3 -m unittest discover -s platform/vm/tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402


class Needs(unittest.TestCase):
    def test_ops_always_joins(self):
        self.assertEqual(vm.network_needs(web=False, postgres=False, views=False), ["ops"])

    def test_a_web_app_with_everything(self):
        self.assertEqual(vm.network_needs(web=True, postgres=True, views=True), ["traefik", "ops", "gateway", "appdata-db"])

    def test_a_job_with_views_gets_no_traefik(self):
        self.assertEqual(vm.network_needs(web=False, postgres=False, views=True), ["ops", "gateway"])

    def test_every_need_has_a_known_alias(self):
        for svc in vm.network_needs(True, True, True):
            self.assertIn(svc, vm.PLATFORM_ALIASES)
        self.assertEqual(vm.PLATFORM_ALIASES["gateway"], "gateway")  # GATEWAY_URL is http://gateway:3000
        self.assertEqual(vm.PLATFORM_ALIASES["ops"], "ops")  # OPS_URL is http://ops:8000
        self.assertEqual(vm.PLATFORM_ALIASES["appdata-db"], "appdata-db")  # DATABASE_URL's host
        self.assertEqual(vm.platform_container("traefik"), "annex-traefik-1")

    def test_from_a_record(self):
        rec = {"type": "web+job", "postgres": True, "views": ["a"], "egress": ["api.trello.com"]}
        self.assertEqual(vm.record_needs(rec), {"web": True, "postgres": True, "views": True, "egress": True})
        self.assertEqual(vm.record_needs({"type": "job", "postgres": False, "views": [], "egress": []}),
                         {"web": False, "postgres": False, "views": False, "egress": False})

    def test_a_record_from_before_d88_has_no_egress_and_maybe_no_type(self):
        self.assertEqual(vm.record_needs({"url": "https://x", "postgres": True, "views": []}),
                         {"web": True, "postgres": True, "views": False, "egress": False})

    def test_plan_connects_missing_and_disconnects_extra_platform_containers(self):
        self.assertEqual(vm.sync_plan(["traefik", "ops", "gateway"], {"ops", "appdata-db", "hooks"}),
                         (["traefik", "gateway"], ["appdata-db", "hooks"]))
        self.assertEqual(vm.sync_plan(["ops"], {"ops"}), ([], []))

    def test_names(self):
        self.assertEqual((vm.app_network("x-y"), vm.egress_network("x-y")), ("annex-app-x-y", "annex-egress-x-y"))


class Subnets(unittest.TestCase):
    POOL = "10.213.0.0/16"

    def test_the_lowest_free_24(self):
        self.assertEqual(vm.pick_subnet(self.POOL, []), "10.213.0.0/24")
        self.assertEqual(vm.pick_subnet(self.POOL, ["10.213.0.0/24", "10.213.2.0/24"]), "10.213.1.0/24")

    def test_networks_outside_the_pool_and_ipv6_are_ignored(self):
        self.assertEqual(vm.pick_subnet(self.POOL, ["172.18.0.0/16", "fd00::/64", "not a subnet"]), "10.213.0.0/24")

    def test_a_larger_network_inside_the_pool_takes_every_24_it_covers(self):
        self.assertEqual(vm.pick_subnet(self.POOL, ["10.213.0.0/23"]), "10.213.2.0/24")

    def test_a_network_that_covers_the_whole_pool(self):
        with self.assertRaises(vm.Fail) as cm:
            vm.pick_subnet(self.POOL, ["10.0.0.0/8"])
        self.assertIn("no free /24", str(cm.exception))

    def test_a_full_pool_says_what_to_do(self):
        used = [f"10.213.{i}.0/24" for i in range(256)]
        with self.assertRaises(vm.Fail) as cm:
            vm.pick_subnet(self.POOL, used)
        self.assertIn("remove apps", str(cm.exception))

    def test_a_bad_pool(self):
        for pool in ("10.213.0.0/25", "nonsense", "10.213.0.1/16", "fd00::/48"):
            with self.subTest(pool=pool), self.assertRaises(vm.Fail):
                vm.pick_subnet(pool, [])

    def test_a_small_pool(self):
        self.assertEqual(vm.pick_subnet("10.213.4.0/23", ["10.213.4.0/24"]), "10.213.5.0/24")


if __name__ == "__main__":
    unittest.main()
