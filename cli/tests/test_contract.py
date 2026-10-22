"""The contract is served in parts (D74): the core, and topics only when an app needs them."""
import unittest

try:
    from annex_cli import ops
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class Contract(unittest.TestCase):
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
