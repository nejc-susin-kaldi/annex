"""Per-app networks (D88): the egress check in the manifest."""
import tempfile
import unittest
from pathlib import Path

try:
    from annex_cli import manifest as mf
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


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
