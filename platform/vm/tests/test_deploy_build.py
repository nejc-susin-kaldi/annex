"""Build limits (D95), on the VM since D101: free-disk and image-size verdicts are pure; build() applies them; apps get
a pids_limit; a failed cleanup is a warning. Moved from toolkit/cli/tests/test_build_limits.py.
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s platform/vm/tests"""
import unittest
import unittest.mock

from deploy_fakes import MB, SETTINGS, Base, dep, manifest, vm

GB = 1024 ** 3


class Verdicts(unittest.TestCase):
    def test_enough_free_space_passes_and_the_boundary_is_enough(self):
        self.assertIsNone(dep.build_space_verdict(50 * GB, 3))
        self.assertIsNone(dep.build_space_verdict(3 * GB, 3))

    def test_too_little_space_names_the_amounts_and_the_commands(self):
        msg = dep.build_space_verdict(int(1.5 * GB), 3)
        self.assertIn("1.5 GB free", msg)
        self.assertIn("at least 3 GB", msg)
        self.assertIn("docker system df", msg)
        self.assertIn("docker image prune", msg)
        self.assertIn("unchanged", msg)

    def test_a_lowered_or_fractional_minimum_is_honoured(self):
        self.assertIsNone(dep.build_space_verdict(1 * GB, 0.5))
        self.assertIsNotNone(dep.build_space_verdict(1 * GB, 1.5))

    def test_a_small_image_passes_and_the_boundary_is_allowed(self):
        self.assertIsNone(dep.image_size_verdict(100 * MB, 2048, "demo"))
        self.assertIsNone(dep.image_size_verdict(2048 * MB, 2048, "demo"))

    def test_a_big_image_is_refused_naming_size_limit_and_app(self):
        msg = dep.image_size_verdict(2500 * MB, 2048, "demo")
        self.assertIn("The image of demo is 2500 MB", msg)
        self.assertIn("limit of 2048 MB", msg)
        self.assertIn("removed", msg)

    def test_defaults_are_3_gb_and_2_gb(self):
        self.assertEqual((dep.BUILD_MIN_FREE_GB, dep.IMAGE_MAX_MB), (3, 2048))


class Limits(Base):
    def test_default_setting_and_caller_override_in_that_order(self):
        self.assertEqual(dep.limit(self.make_run(), "ANNEX_IMAGE_MAX_MB", 2048), 2048)
        run = self.make_run(settings={**self.settings(), "ANNEX_IMAGE_MAX_MB": "512"})
        self.assertEqual(dep.limit(run, "ANNEX_IMAGE_MAX_MB", 2048), 512)
        run = self.make_run(meta={"overrides": {"ANNEX_IMAGE_MAX_MB": "50"}},
                            settings={**self.settings(), "ANNEX_IMAGE_MAX_MB": "512"})
        self.assertEqual(dep.limit(run, "ANNEX_IMAGE_MAX_MB", 2048), 50)

    def test_an_empty_setting_means_the_default_and_a_bad_one_is_refused(self):
        run = self.make_run(settings={**self.settings(), "ANNEX_IMAGE_MAX_MB": ""})
        self.assertEqual(dep.limit(run, "ANNEX_IMAGE_MAX_MB", 7), 7)
        for bad in ("lots", "-5"):
            run = self.make_run(settings={**self.settings(), "ANNEX_IMAGE_MAX_MB": bad})
            with self.assertRaises(vm.Fail) as cm:
                dep.limit(run, "ANNEX_IMAGE_MAX_MB", 7)
            self.assertIn("ANNEX_IMAGE_MAX_MB", str(cm.exception))

    def test_a_bad_health_timeout_is_refused(self):
        with self.assertRaises(vm.Fail):
            dep.health_timeout(self.make_run(meta={"overrides": {"ANNEX_HEALTH_TIMEOUT": "soon"}}))
        self.assertEqual(dep.health_timeout(self.make_run()), 60)

    def settings(self):
        from deploy_fakes import SETTINGS
        return dict(SETTINGS)


class Build(Base):
    def free(self, n):
        return unittest.mock.patch.object(dep.shutil, "disk_usage", return_value=unittest.mock.Mock(free=n))

    def test_a_small_image_with_room_builds(self):
        dep.build(self.make_run(), manifest(), "annex-app/demo:r1")
        self.assertTrue(self.docker.ran("docker", "build"))
        self.assertFalse(self.docker.ran("docker", "rmi"))
        self.assertTrue((self.base / "deploy" / "demo" / "build.log").exists())

    def test_low_disk_refuses_before_docker_build_with_the_message(self):
        with self.free(1 * GB), self.assertRaises(vm.Fail) as cm:
            dep.build(self.make_run(), manifest(), "annex-app/demo:r1")
        self.assertIn("docker system df", str(cm.exception))
        self.assertFalse(self.docker.ran("docker", "build"))

    def test_the_minimum_comes_from_the_platform_settings(self):
        from deploy_fakes import SETTINGS
        with self.free(1 * GB):
            dep.build(self.make_run(settings={**SETTINGS, "ANNEX_BUILD_MIN_FREE_GB": "0.5"}), manifest(), "i:r1")
        self.assertTrue(self.docker.ran("docker", "build"))

    def test_a_disk_that_cannot_answer_does_not_block(self):
        with unittest.mock.patch.object(dep.shutil, "disk_usage", side_effect=OSError("no")):
            dep.build(self.make_run(), manifest(), "annex-app/demo:r1")
        self.assertTrue(self.docker.ran("docker", "build"))

    def test_a_big_image_is_removed_and_refused_with_its_size(self):
        from deploy_fakes import SETTINGS
        self.docker.size = 300 * MB
        with self.assertRaises(vm.Fail) as cm:
            dep.build(self.make_run(settings={**SETTINGS, "ANNEX_IMAGE_MAX_MB": "200"}), manifest(), "annex-app/demo:r1")
        self.assertIn("300 MB", str(cm.exception))
        self.assertIn("200 MB", str(cm.exception))
        self.assertTrue(self.docker.ran("docker", "rmi", "annex-app/demo:r1"))

    def test_the_caller_lowers_the_cap(self):
        with self.assertRaises(vm.Fail):
            dep.build(self.make_run(meta={"overrides": {"ANNEX_IMAGE_MAX_MB": "50"}}), manifest(), "annex-app/demo:r1")

    def test_a_root_image_is_removed_and_refused(self):
        self.docker.user = "root"
        with self.assertRaises(vm.Fail) as cm:
            dep.build(self.make_run(), manifest(), "annex-app/demo:r1")
        self.assertIn("runs as root", str(cm.exception))
        self.assertTrue(self.docker.ran("docker", "rmi", "annex-app/demo:r1"))

    def test_platform_labels_are_refused(self):
        self.docker.labels = '{"traefik.enable": "true"}'
        with self.assertRaises(vm.Fail) as cm:
            dep.build(self.make_run(), manifest(), "annex-app/demo:r1")
        self.assertIn("traefik.enable", str(cm.exception))


class Compose(Base):
    def test_every_app_container_has_a_pids_limit(self):
        for kw in ({}, {"type": "job", "port": None, "healthcheck": None}):
            c = dep.compose_for(self.make_run(), manifest(**kw), "annex-app/demo:r1", 1)
            self.assertEqual(c["services"]["demo"]["pids_limit"], 256)


class Cleanup(Base):
    def test_a_failed_cleanup_is_returned_as_a_warning(self):
        run = self.make_run()
        with unittest.mock.patch.object(dep, "prune", side_effect=vm.Fail("disk on fire")):
            got = dep.cleanup(run, "annex-app/demo:r1")
        self.assertEqual(len(got), 1)
        self.assertIn("disk on fire", got[0])
        self.assertIn("Cleaning up", got[0])
        self.assertIn("Warning: Cleaning up", run.out.getvalue())

    def test_a_good_cleanup_returns_nothing_and_keeps_the_last_three_live_images(self):
        vm.write_releases(self.base, "demo", [
            {"release": i, "image": f"annex-app/demo:r{i}", "result": "live" if i != 4 else "failed"} for i in range(1, 6)])
        self.docker.images = [f"annex-app/demo:r{i}" for i in range(1, 7)]
        self.assertEqual(dep.cleanup(self.make_run(), "annex-app/demo:r6"), [])
        [rmi] = self.docker.ran("docker", "rmi")
        # kept: r6 (going live), r5 and r3 (the failed r4 doesn't count)
        self.assertEqual(sorted(rmi[2:]), ["annex-app/demo:r1", "annex-app/demo:r2", "annex-app/demo:r4"])
        self.assertTrue(self.docker.ran("docker", "builder"))  # the VM is Annex's alone: its build cache is trimmed

    def test_the_local_stack_prunes_only_annex_images(self):
        # D99: a Mac's Docker holds the user's own images and build cache too
        dep.cleanup(self.make_run(settings={**SETTINGS, "ANNEX_TARGET": "local"}), "annex-app/demo:r1")
        [prune] = [a for a in self.docker.ran("docker", "image") if "prune" in a]
        self.assertIn("label=com.kaldi.annex=true", prune)
        self.assertEqual(self.docker.ran("docker", "builder"), [])


if __name__ == "__main__":
    unittest.main()
