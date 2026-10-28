"""D103: the CLI reports its commit, a dirty flag and its contract version in the deploy's meta; the VM decides."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from annex_cli import ops
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


def make_repo(d: str) -> Path:
    repo = Path(d)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", d, *args], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", d, "add", "f"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "c"], check=True)
    return repo


class CliVersion(unittest.TestCase):
    def test_commit_clean_and_dirty(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            sha = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            with mock.patch.object(ops, "REPO", repo):
                self.assertEqual(ops.cli_version(), {"commit": sha, "dirty": False, "contract_version": ops.contract_version()})
                (repo / "f").write_text("changed")
                self.assertTrue(ops.cli_version()["dirty"])

    def test_not_a_checkout_is_unknown(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ops, "REPO", Path(d)):
            self.assertEqual(ops.cli_version(), {"commit": "unknown", "dirty": False, "contract_version": ops.contract_version()})

    def test_label(self):
        self.assertEqual(ops.cli_label({"commit": "a" * 40, "dirty": True}), "a" * 12 + "+dirty")
        self.assertEqual(ops.cli_label({"commit": "unknown", "dirty": False}), "unknown")
        self.assertIsNone(ops.cli_label(None))
        self.assertIsNone(ops.cli_label({}))

    def test_deploy_meta_carries_cli_and_the_contract_version(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "annex.yaml").write_text("name: demo\nowner: sam\ntype: web\nport: 8080\nhealthcheck: /healthz\n")
            (Path(d) / "Dockerfile").write_text("FROM scratch\nUSER 10001\n")
            sent = {}

            class FakeRemote:
                def vm(self, *args):
                    return {"views": []}

                def call(self, *args, input=None, progress=None, **kw):
                    sent["body"] = input
                    return {"ok": True, "release": 1}

            with mock.patch.object(ops, "Remote", FakeRemote):
                ops.deploy(Path(d))
            meta = json.loads(sent["body"].split(b"\n", 1)[0])
            self.assertEqual(set(meta["cli"]), {"commit", "dirty", "contract_version"})
            self.assertEqual(meta["cli"]["contract_version"], ops.contract_version())
            self.assertEqual(meta["line"]["contract_version"], ops.contract_version())
