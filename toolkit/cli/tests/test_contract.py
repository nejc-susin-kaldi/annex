"""The contract is served in parts (D74): the core, and topics only when an app needs them."""
import unittest
import unittest.mock

try:
    from annex_cli import ops
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class Contract(unittest.TestCase):
    def setUp(self):
        # the core asks the instance for its views (D119): never a real one from a unit test
        p = unittest.mock.patch.object(ops, "_live_views", return_value="### The views on this instance\nNone.\n")
        p.start()
        self.addCleanup(p.stop)

    def test_every_topic_heading_exists(self):
        # renaming a heading in contract.md must not silently drop a topic
        _, sections = ops._contract_sections()
        headings = {h for h, _ in sections}
        for topic, (heading, _) in ops.CONTRACT_TOPICS.items():
            self.assertIn(heading, headings, f"topic {topic}: no '## {heading}' in contract.md")

    def test_core_leaves_topics_out_and_points_at_them(self):
        core = ops.contract()
        for heading, _ in ops.CONTRACT_TOPICS.values():
            self.assertNotIn(f"\n## {heading}\n", core)
        for topic in ops.CONTRACT_TOPICS:
            self.assertIn(f"annex contract {topic}", core)
        for must in ("## annex.yaml", "## Runtime rules", "## Injected environment", "## Gateway views", "## Commands"):
            self.assertIn(must, core)

    def test_cross_site_rule_is_core(self):
        # hardening A4: every app gets it, so it is not a topic
        self.assertIn("\n## Cross-site requests\n", ops.contract())
        self.assertNotIn("Cross-site requests", [h for h, _ in ops.CONTRACT_TOPICS.values()])
        self.assertIn("Sec-Fetch-Site", ops.contract())

    def test_core_is_about_half(self):
        self.assertLess(len(ops.contract().split()), 0.6 * len(ops.contract("all").split()))

    def test_topics_and_errors(self):
        self.assertTrue(ops.contract("webhooks").startswith("## Webhooks"))
        with self.assertRaises(ops.AnnexError) as e:
            ops.contract("nope")
        self.assertIn("notify, webhooks, logging, stack", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class LiveViews(unittest.TestCase):
    """The views section ends with the instance's own views, asked live (D119)."""
    def test_block(self):
        none = ops.views_block({"about": None, "views": []})
        self.assertIn("no data views", none)
        self.assertIn("data.views: []", none)
        some = ops.views_block({"about": "Goat Arcade: a chain of arcades.", "views": [
            {"name": "venues", "comment": "The open arcades.", "columns": ["venue_code", "city"]},
            {"name": "x", "comment": None, "columns": None}]})
        self.assertIn("Goat Arcade: a chain of arcades.", some)
        self.assertIn("| `venues` | The open arcades. | venue_code, city |", some)
        self.assertIn("| `x` | - |  |", some)
        self.assertIn("couldn't be asked (no route)", ops.views_block(None, "no route"))

    def test_core_and_all_carry_it_inside_the_views_section(self):
        with unittest.mock.patch.object(ops, "_live_views", return_value="### The views on this instance\nLIVE\n"):
            for text in (ops.contract(), ops.contract("all")):
                views_at, live_at = text.index("## Gateway views"), text.index("LIVE")
                self.assertLess(views_at, live_at)
                self.assertLess(live_at, text.index("\n## Commands"))

    def test_older_platform_gives_names_only(self):
        calls = []

        class Fake:
            def vm(self, *args):
                calls.append(args)
                if args == ("views-detail",):
                    raise ops.AnnexError("annex_vm views-detail failed: argument cmd: invalid choice: 'views-detail'")
                return {"views": ["venues"]}
        with unittest.mock.patch.object(ops, "Remote", Fake):
            self.assertIn("| `venues` | - |  |", ops._live_views())
        self.assertEqual(calls, [("views-detail",), ("views",)])

    def test_not_connected_reaches_the_agent(self):
        with unittest.mock.patch.object(ops, "Remote", side_effect=ops.AnnexError("Annex isn't pointed at an instance")):
            self.assertIn("isn't pointed at an instance", ops._live_views())
