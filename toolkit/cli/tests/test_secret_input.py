"""annex secret set: where the value comes from (stdin, --from-file, KEY=VALUE with a warning)."""
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from annex_cli.main import secret_pairs  # noqa: E402
from annex_cli.remote import AnnexError  # noqa: E402


class SecretPairsTest(unittest.TestCase):
    def test_bare_key_reads_stdin_and_strips_one_newline(self):
        self.assertEqual(secret_pairs(["TOKEN"], None, io.StringIO("s3cr=et\n")), (["TOKEN=s3cr=et"], []))

    def test_stdin_without_newline(self):
        self.assertEqual(secret_pairs(["TOKEN"], None, io.StringIO("abc"))[0], ["TOKEN=abc"])

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "v"
            f.write_text("filevalue\n")
            self.assertEqual(secret_pairs(["TOKEN"], str(f), io.StringIO("ignored"))[0], ["TOKEN=filevalue"])

    def test_key_value_still_works_but_warns(self):
        pairs, warnings = secret_pairs(["A=1", "B=2"], None, io.StringIO(""))
        self.assertEqual(pairs, ["A=1", "B=2"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("shell history", warnings[0])
        self.assertIn("A, B", warnings[0])

    def test_refuses_empty_multiline_and_several_bare_keys(self):
        for text in ("", "\n", "a\nb\n"):
            with self.assertRaises(AnnexError):
                secret_pairs(["TOKEN"], None, io.StringIO(text))
        with self.assertRaises(AnnexError):
            secret_pairs(["A", "B"], None, io.StringIO("x"))
        with self.assertRaises(AnnexError):
            secret_pairs(["A=1"], "somefile", io.StringIO(""))
        with self.assertRaises(AnnexError):
            secret_pairs(["TOKEN"], "/nonexistent/file", io.StringIO(""))


if __name__ == "__main__":
    unittest.main()
