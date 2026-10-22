"""Build limits (D95): free-disk and image-size verdicts are pure; _build applies them; apps get a pids_limit."""
import os
import subprocess
import unittest
from unittest import mock

try:
    from annex_cli import manifest as mf
    from annex_cli import ops
    from annex_cli.remote import AnnexError
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")

GB, MB = 1024 ** 3, 1024 ** 2


class FakeRemote:
    """Answers the commands _build sends; records them."""

    def __init__(self, free=100 * GB, size=100 * MB, settings=None, build_rc=0):
        self.free, self.size, self.settings, self.build_rc = free, size, settings or {}, build_rc
        self.data, self.cmds = "/srv/annex", []

    def path(self, *parts):
        return "/".join((self.data, *parts))

    def url_for(self, app):
        return f"https://{app}.annex.home.arpa"

    def host_for(self, app):
        return f"{app}.annex.home.arpa"

    def run(self, cmd, check=True, **kw):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, self.build_rc if "docker build" in cmd else 0, b"", b"")

    def out(self, cmd, check=True, **kw):
        self.cmds.append(cmd)
        if cmd.startswith("df "):
            return f"{self.free}\n"
        if ".Size" in cmd:
            return f"{self.size}\n"
        if ".Config.User" in cmd:
            return "10001\n"
        return "null\n"  # labels

    def ran(self, text):
        return any(text in c for c in self.cmds)


def manifest(**kw):
    base = dict(name="demo", owner="sam", description="", type="web", port=8080, healthcheck="/healthz",
                postgres=False, files=False, views=[], schedule=[], env={}, secrets=[], egress=[])
    return mf.Manifest(**{**base, **kw})


class Verdicts(unittest.TestCase):
    def test_enough_free_space_passes_and_the_boundary_is_enough(self):
        self.assertIsNone(ops.build_space_verdict(50 * GB, 3))
        self.assertIsNone(ops.build_space_verdict(3 * GB, 3))

    def test_too_little_space_names_the_amounts_and_the_commands(self):
        msg = ops.build_space_verdict(int(1.5 * GB), 3)
        self.assertIn("1.5 GB free", msg)
        self.assertIn("at least 3 GB", msg)
        self.assertIn("docker system df", msg)
        self.assertIn("docker image prune", msg)
        self.assertIn("unchanged", msg)

    def test_a_lowered_or_fractional_minimum_is_honoured(self):
        self.assertIsNone(ops.build_space_verdict(1 * GB, 0.5))
        self.assertIsNotNone(ops.build_space_verdict(1 * GB, 1.5))

    def test_a_small_image_passes_and_the_boundary_is_allowed(self):
        self.assertIsNone(ops.image_size_verdict(100 * MB, 2048, "demo"))
        self.assertIsNone(ops.image_size_verdict(2048 * MB, 2048, "demo"))

    def test_a_big_image_is_refused_naming_size_limit_and_app(self):
        msg = ops.image_size_verdict(2500 * MB, 2048, "demo")
        self.assertIn("The image of demo is 2500 MB", msg)
        self.assertIn("limit of 2048 MB", msg)
        self.assertIn("removed", msg)

    def test_defaults_are_3_gb_and_2_gb(self):
        self.assertEqual((ops.BUILD_MIN_FREE_GB, ops.IMAGE_MAX_MB), (3, 2048))


class Limits(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ANNEX_IMAGE_MAX_MB", None)

    def tearDown(self):
        os.environ.pop("ANNEX_IMAGE_MAX_MB", None)

    def test_default_setting_and_environment_override_in_that_order(self):
        self.assertEqual(ops._limit(FakeRemote(), "ANNEX_IMAGE_MAX_MB", 2048), 2048)
        r = FakeRemote(settings={"ANNEX_IMAGE_MAX_MB": "512"})
        self.assertEqual(ops._limit(r, "ANNEX_IMAGE_MAX_MB", 2048), 512)
        os.environ["ANNEX_IMAGE_MAX_MB"] = "50"
        self.assertEqual(ops._limit(r, "ANNEX_IMAGE_MAX_MB", 2048), 50)

    def test_an_empty_setting_means_the_default_and_a_bad_one_is_refused(self):
        self.assertEqual(ops._limit(FakeRemote(settings={"ANNEX_IMAGE_MAX_MB": ""}), "ANNEX_IMAGE_MAX_MB", 7), 7)
        for bad in ("lots", "-5"):
            with self.assertRaises(AnnexError) as cm:
                ops._limit(FakeRemote(settings={"ANNEX_IMAGE_MAX_MB": bad}), "ANNEX_IMAGE_MAX_MB", 7)
            self.assertIn("ANNEX_IMAGE_MAX_MB", str(cm.exception))


class Build(unittest.TestCase):
    def setUp(self):
        for k in ("ANNEX_IMAGE_MAX_MB", "ANNEX_BUILD_MIN_FREE_GB"):
            os.environ.pop(k, None)

    tearDown = setUp

    def test_a_small_image_with_room_builds(self):
        r = FakeRemote()
        ops._build(r, manifest(), "annex-app/demo:r1")
        self.assertTrue(r.ran("docker build"))
        self.assertFalse(r.ran("docker rmi"))

    def test_low_disk_refuses_before_docker_build_with_the_message(self):
        r = FakeRemote(free=1 * GB)
        with self.assertRaises(AnnexError) as cm:
            ops._build(r, manifest(), "annex-app/demo:r1")
        self.assertIn("docker system df", str(cm.exception))
        self.assertFalse(r.ran("docker build"))

    def test_the_minimum_comes_from_the_platform_settings(self):
        r = FakeRemote(free=1 * GB, settings={"ANNEX_BUILD_MIN_FREE_GB": "0.5"})
        ops._build(r, manifest(), "annex-app/demo:r1")
        self.assertTrue(r.ran("docker build"))

    def test_a_df_that_cannot_answer_does_not_block(self):
        r = FakeRemote(free="")
        ops._build(r, manifest(), "annex-app/demo:r1")
        self.assertTrue(r.ran("docker build"))

    def test_a_big_image_is_removed_and_refused_with_its_size(self):
        r = FakeRemote(size=300 * MB, settings={"ANNEX_IMAGE_MAX_MB": "200"})
        with self.assertRaises(AnnexError) as cm:
            ops._build(r, manifest(), "annex-app/demo:r1")
        self.assertIn("300 MB", str(cm.exception))
        self.assertIn("200 MB", str(cm.exception))
        self.assertTrue(r.ran("docker rmi annex-app/demo:r1"))

    def test_the_environment_lowers_the_cap(self):
        os.environ["ANNEX_IMAGE_MAX_MB"] = "50"
        with self.assertRaises(AnnexError):
            ops._build(FakeRemote(size=100 * MB), manifest(), "annex-app/demo:r1")


class Compose(unittest.TestCase):
    def test_every_app_container_has_a_pids_limit(self):
        for kw in ({}, {"type": "job", "port": None, "healthcheck": None}):
            c = ops.compose_for(manifest(**kw), FakeRemote(), "annex-app/demo:r1", 1)
            self.assertEqual(c["services"]["demo"]["pids_limit"], 256)


class Cleanup(unittest.TestCase):
    def test_a_failed_cleanup_is_returned_as_a_warning(self):
        shown = []
        with mock.patch.object(ops, "_prune", side_effect=AnnexError("disk on fire")):
            got = ops._cleanup(FakeRemote(), "demo", "annex-app/demo:r1", shown.append)
        self.assertEqual(len(got), 1)
        self.assertIn("disk on fire", got[0])
        self.assertIn("Cleaning up", got[0])
        self.assertTrue(shown and shown[0].startswith("Warning"))

    def test_a_good_cleanup_returns_nothing(self):
        with mock.patch.object(ops, "_prune"):
            self.assertEqual(ops._cleanup(FakeRemote(), "demo", "x"), [])


if __name__ == "__main__":
    unittest.main()
