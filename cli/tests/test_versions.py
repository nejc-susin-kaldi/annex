"""C4: the contract and runtime versions, and the deploy's runtime warning."""
import tempfile
import unittest
from pathlib import Path

try:
    from annex_cli import ops
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class Versions(unittest.TestCase):
    def test_contract_version_line(self):
        self.assertEqual(ops.contract_version(), 2)
        self.assertIn("ANNEX_CONTRACT_VERSION: 2", ops.contract())
        self.assertIn("ANNEX_CONTRACT_VERSION: 2", ops.contract("all"))

    def test_topics_and_core_unchanged(self):
        self.assertEqual(list(ops.CONTRACT_TOPICS), ["notify", "webhooks", "logging", "stack"])
        core = ops.contract()
        self.assertTrue(core.startswith("# Annex app contract"))
        for heading, _ in ops.CONTRACT_TOPICS.values():
            self.assertNotIn(f"\n## {heading}\n", core)
        self.assertIn("\n## Cross-site requests\n", core)

    def test_runtime_version_parsing(self):
        self.assertEqual(ops.runtime_version('"""doc"""\n__version__ = 7  # c\n'), 7)
        self.assertEqual(ops.runtime_version("import x\nexport const __version__ = 12; // c\n"), 12)
        self.assertEqual(ops.runtime_version("no version here"), 0)
        self.assertEqual(ops.runtime_version(None), 0)
        self.assertEqual(ops.runtime_version("    __version__ = 3\n"), 0)  # only a module-level assignment counts

    def test_shipped_runtime_has_a_version(self):
        for name in ("annex_runtime.py", "annex_runtime.js"):
            self.assertGreaterEqual(ops.runtime_version((ops.RUNTIME / name).read_text()), 1, name)

    def test_message_old_current_new_and_unversioned(self):
        self.assertIsNone(ops.runtime_message("annex_runtime.py", 2, 2, True))
        old = ops.runtime_message("annex_runtime.py", 1, 2, False)
        self.assertIn("your runtime is v1, the current one is v2", old)
        self.assertNotIn("differs", old)
        v0 = ops.runtime_message("annex_runtime.js", 0, 2, False)
        self.assertIn("your runtime is v0 (before versions), the current one is v2", v0)
        self.assertIn("newer than the platform's v2", ops.runtime_message("annex_runtime.py", 3, 2, False))
        self.assertIn("v2, the current version, but the file differs", ops.runtime_message("annex_runtime.py", 2, 2, False))

    def test_runtime_versions_of_a_folder(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "annex_runtime.py").write_text("__version__ = 1\n")
            self.assertEqual(ops.runtime_versions(Path(d)), {"annex_runtime.py": 1})
            (Path(d) / "annex_runtime.js").write_text("// old copy\n")
            self.assertEqual(ops.runtime_versions(Path(d)), {"annex_runtime.py": 1, "annex_runtime.js": 0})
        self.assertEqual(ops.runtime_versions(None), {})
