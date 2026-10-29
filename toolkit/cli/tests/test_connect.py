"""`annex connect` (D119): it asks the instance over SSH, then writes this user's pointer; nothing on a failure."""
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

try:
    from annex_cli import ops
    from annex_cli.remote import AnnexError, Remote, target_settings
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")

FOUND = b"ANNEX_URL_HOME=https://home.annex.kaldi.si\nANNEX_HOST_PATTERN={name}.annex.kaldi.si\ncommit=abc\ncontract=2\n"


class Connect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "conf" / "annex" / "instance"
        self.env = {"ANNEX_INSTANCE_FILE": str(self.file)}
        self.scripts = []

    def run_with(self, out: bytes, code: int = 0):
        def fake(r, script, **kw):
            self.scripts.append((r.host, script))
            return subprocess.CompletedProcess([], code, out, b"ssh: Could not resolve hostname" if code else b"")
        return unittest.mock.patch.object(Remote, "run", fake)

    def test_writes_the_pointer_the_cli_then_follows(self):
        with self.run_with(FOUND), unittest.mock.patch.object(ops, "contract_version", return_value=2):
            res = ops.connect("annex-kaldi", "/srv/annex", self.env)
        self.assertEqual((res["home"], res["notes"]), ("https://home.annex.kaldi.si", []))
        self.assertEqual(self.scripts, [("annex-kaldi", "cat /srv/annex/platform/.env /srv/annex/platform/VERSION")])
        t = target_settings(self.env)
        self.assertEqual((t["ANNEX_SSH_HOST"], t["ANNEX_DATA_DIR"]), ("annex-kaldi", "/srv/annex"))
        c = ops.connection(self.env)
        self.assertEqual((c["connected"], c["home"]), (True, "https://home.annex.kaldi.si"))

    def test_nothing_written_when_the_instance_does_not_answer(self):
        with self.run_with(b"", code=255), self.assertRaises(AnnexError) as e:
            ops.connect("annex-kaldi", "/srv/annex", self.env)
        self.assertIn("ssh annex-kaldi true", str(e.exception))
        self.assertFalse(self.file.exists())

    def test_contract_versions_are_noted(self):
        with self.run_with(FOUND), unittest.mock.patch.object(ops, "contract_version", return_value=3):
            self.assertIn("newer than the platform", ops.connect("annex-kaldi", "/srv/annex", self.env)["notes"][0])

    def test_bad_arguments_never_reach_a_shell(self):
        with self.run_with(FOUND):
            for host, data in (("x; id", "/srv/annex"), ("-oProxyCommand=x", "/srv/annex"), ("h", "srv"), ("h", "/a/../b"),
                               ("h", "/srv/annex; id")):
                with self.subTest(host=host, data=data), self.assertRaises(AnnexError):
                    ops.connect(host, data, self.env)
        self.assertEqual(self.scripts, [])

    def test_not_connected(self):
        self.assertFalse(ops.connection(self.env)["connected"])


if __name__ == "__main__":
    unittest.main()
