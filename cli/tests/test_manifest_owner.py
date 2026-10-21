"""annex.yaml's owner is a login and description is one short line (D80).
PYTHONPATH=cli/src python3 -m unittest discover -s cli/tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from annex_cli import manifest as mf
except ImportError as e:  # PyYAML missing in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


def problems(owner: str = "sam", description: str | None = None) -> list[str]:
    d = Path(tempfile.mkdtemp())
    text = f"name: demo-app\nowner: {owner}\ntype: job\nschedule:\n  - {{name: j, cron: '0 6 * * *', command: 'true'}}\n"
    if description is not None:
        import json
        text += f"description: {json.dumps(description)}\n"
    (d / "annex.yaml").write_text(text)
    try:
        mf.load(d)
    except mf.ManifestError as e:
        return e.problems
    return []


class Owner(unittest.TestCase):
    def test_a_login_passes(self):
        self.assertEqual([p for p in problems("sam.k-2") if p.startswith("owner")], [])

    def test_free_text_is_refused_and_says_why(self):
        for bad in ("Sam", "CHANGE-ME", "sam k", "9sam", "a" * 33):
            got = [p for p in problems(bad) if p.startswith("owner")]
            self.assertEqual(len(got), 1, bad)
            self.assertIn("console", got[0])


class Description(unittest.TestCase):
    def test_short_is_fine(self):
        self.assertEqual([p for p in problems(description="x" * 200) if p.startswith("description")], [])

    def test_too_long(self):
        self.assertEqual(len([p for p in problems(description="x" * 201) if p.startswith("description")]), 1)

    def test_control_characters(self):
        for bad in ("two\nlines", "tab\there", "bell\x07"):
            self.assertEqual(len([p for p in problems(description=bad) if p.startswith("description")]), 1, repr(bad))


if __name__ == "__main__":
    unittest.main()
