"""Version coupling and the one manifest module (control-plane V3, D103): the contract-version rule as a pure
function, the VM's re-validation of the uploaded annex.yaml, and the cli commit in the release line.
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s platform/vm/tests"""
import re
import tempfile
import unittest
from pathlib import Path

from deploy_fakes import Base, dep, deploy_stdin, manifest, source_tar, yaml_for

ROOT = Path(__file__).resolve().parents[3]  # the repo
CLI = {"commit": "a" * 40, "dirty": False, "contract_version": 3}
PLATFORM = {"commit": "b" * 40, "dirty": False, "contract": 3}


class CheckVersions(unittest.TestCase):
    def test_equal_contracts_pass_silently_whatever_the_commits(self):
        self.assertEqual(dep.check_versions(CLI, PLATFORM), (None, []))

    def test_a_newer_cli_is_refused_and_told_to_push_the_platform(self):
        refusal, notes = dep.check_versions({**CLI, "contract_version": 4}, PLATFORM)
        self.assertIn("contract v4 > v3", refusal)
        self.assertIn("scripts/push.sh", refusal)
        self.assertIn("aaaaaaaaaaaa", refusal)  # both commits are named, shortened
        self.assertIn("bbbbbbbbbbbb", refusal)
        self.assertEqual(notes, [])

    def test_an_older_cli_deploys_with_a_note(self):
        refusal, notes = dep.check_versions({**CLI, "contract_version": 2}, PLATFORM)
        self.assertIsNone(refusal)
        self.assertEqual(len(notes), 1)
        self.assertIn("contract v2 < v3", notes[0])
        self.assertIn("update your checkout", notes[0])

    def test_dirty_trees_are_named(self):
        refusal, _ = dep.check_versions({**CLI, "dirty": True, "contract_version": 9}, {**PLATFORM, "dirty": True})
        self.assertEqual(refusal.count("(modified)"), 2)

    def test_unknown_numbers_check_nothing(self):
        self.assertEqual(dep.check_versions(CLI, {}), (None, []))
        self.assertEqual(dep.check_versions(CLI, {**PLATFORM, "contract": None}), (None, []))
        self.assertEqual(dep.check_versions({"commit": "unknown"}, PLATFORM), (None, []))
        self.assertEqual(dep.check_versions({**CLI, "contract_version": True}, PLATFORM), (None, []))

    def test_a_cli_without_a_report_gets_a_note(self):
        refusal, notes = dep.check_versions(None, PLATFORM)
        self.assertIsNone(refusal)
        self.assertIn("doesn't report its version", notes[0])

    def test_the_commit_is_unknown_when_missing(self):
        refusal, _ = dep.check_versions({"contract_version": 5}, {"contract": 3})
        self.assertIn("unknown", refusal)


class PlatformVersions(unittest.TestCase):
    def test_parse_and_read(self):
        stamp = "commit=abc\ndirty=true\ncontract=7\nenv_sha256=ff\n"
        self.assertEqual(dep.parse_version_stamp(stamp)["contract"], "7")
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            self.assertEqual(dep.platform_versions(base), {})
            (base / "platform").mkdir()
            (base / "platform" / "VERSION").write_text(stamp)
            self.assertEqual(dep.platform_versions(base), {"commit": "abc", "dirty": True, "contract": 7})
            (base / "platform" / "VERSION").write_text("commit=abc\ndirty=false\n")  # pushed before the stamp had it
            self.assertIsNone(dep.platform_versions(base)["contract"])


class DeployVersions(Base):
    def stamp(self, contract):
        (self.base / "platform").mkdir(exist_ok=True)
        (self.base / "platform" / "VERSION").write_text(f"commit={'b' * 40}\ndirty=false\ncontract={contract}\n")

    def test_a_newer_cli_is_refused_before_any_release_line(self):
        self.stamp(3)
        lines, _ = self.go("deploy", deploy_stdin(manifest(), cli={**CLI, "contract_version": 4}))
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("Push the platform first: scripts/push.sh", lines[-1]["error"])
        self.assertEqual(self.releases(), [])
        self.assertEqual(self.docker.ran("docker", "build"), [])

    def test_an_older_cli_deploys_and_the_result_carries_the_note(self):
        self.stamp(3)
        lines, _ = self.go("deploy", deploy_stdin(manifest(), cli={**CLI, "contract_version": 2}))
        self.assertTrue(lines[-1]["ok"], lines[-1])
        self.assertTrue(any("older than the platform" in w for w in lines[-1]["warnings"]))
        self.assertTrue(any("note" in x for x in lines))

    def test_the_cli_commit_is_in_the_release_line(self):
        self.stamp(3)
        self.go("deploy", deploy_stdin(manifest(), cli={**CLI, "dirty": True}))
        self.assertEqual(self.releases()[-1]["cli"], {"commit": "a" * 40, "dirty": True})

    def test_no_cli_report_leaves_the_line_without_cli(self):
        self.stamp(3)
        lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertTrue(lines[-1]["ok"])
        self.assertNotIn("cli", self.releases()[-1])


class Revalidation(Base):
    def test_an_invalid_uploaded_annex_yaml_is_refused_with_the_cli_messages(self):
        bad = yaml_for(manifest()).replace("owner: sam\n", "")
        tar = source_tar({"Dockerfile": "FROM scratch\nUSER 10001\n", "annex.yaml": bad})
        lines, _ = self.go("deploy", deploy_stdin(manifest(), tar=tar))
        err = lines[-1]["error"]
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("owner: required. Set it to the builder's login", err)
        self.assertIn("annex.yaml in the upload is not valid on the VM", err)
        self.assertEqual(self.docker.ran("docker", "build"), [])
        self.assertEqual(self.releases()[-1]["result"], "failed")

    def test_a_missing_annex_yaml_is_refused(self):
        tar = source_tar({"Dockerfile": "FROM scratch\nUSER 10001\n"})
        lines, _ = self.go("deploy", deploy_stdin(manifest(), tar=tar))
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("annex.yaml is missing", lines[-1]["error"])

    def test_an_upload_that_differs_from_the_validated_manifest_is_refused(self):
        other = yaml_for(manifest(memory_mb=512, secrets=["TOKEN"]))  # valid, but not what the CLI sent
        tar = source_tar({"Dockerfile": "FROM scratch\nUSER 10001\n", "annex.yaml": other})
        lines, _ = self.go("deploy", deploy_stdin(manifest(), tar=tar))
        err = lines[-1]["error"]
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("the manifest in the upload doesn't match what the CLI validated", err)
        self.assertIn("memory_mb", err)
        self.assertIn("secrets", err)
        self.assertEqual(self.docker.ran("docker", "build"), [])

    def test_the_matching_upload_builds(self):
        lines, _ = self.go("deploy", deploy_stdin(manifest(secrets=["K"], env={"A": "1"})))
        self.assertTrue(lines[-1]["ok"], lines[-1])


class OneSource(unittest.TestCase):
    def test_the_manifest_module_is_only_generated_never_committed_in_vm(self):
        # scripts/render.py copies cli/src/annex_cli/manifest.py into the bundle as vm/annex_manifest.py; a committed
        # copy would be a second rule that could drift
        for name in ("annex_manifest.py", "annex_logcheck.py"):
            self.assertFalse((ROOT / "platform" / "vm" / name).exists(), f"vm/{name} must be generated by scripts/render.py, not committed")
        render = (ROOT / "scripts" / "render.py").read_text()
        self.assertIn('("manifest.py", "annex_manifest.py")', render)

    def test_in_the_checkout_the_deploy_loads_the_cli_module(self):
        self.assertEqual(Path(dep.mf.__file__).resolve(), (ROOT / "cli/src/annex_cli/manifest.py").resolve())

    def test_render_stamps_the_contract_version(self):
        render = (ROOT / "scripts" / "render.py").read_text()
        self.assertIn('f"contract={contract_version()}', render)
        contract = (ROOT / "cli/src/annex_cli/contract.md").read_text()
        self.assertTrue(re.search(r"^ANNEX_CONTRACT_VERSION:\s*\d+\s*$", contract, re.M))


if __name__ == "__main__":
    unittest.main()
