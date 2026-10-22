"""An image may not carry labels that route, schedule, list or back up (D83): the verdict is a pure function."""
import unittest

try:
    from annex_cli import ops
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")

OURS = {"com.kaldi.annex": "true", "com.kaldi.annex.app": "demo"}


class Offenders(unittest.TestCase):
    def test_no_labels_and_the_two_build_labels_pass(self):
        self.assertEqual(ops.platform_label_offenders(None, "demo"), [])
        self.assertEqual(ops.platform_label_offenders({}, "demo"), [])
        self.assertEqual(ops.platform_label_offenders(OURS, "demo"), [])

    def test_ordinary_labels_pass(self):
        labels = {**OURS, "org.opencontainers.image.source": "x", "maintainer": "me"}
        self.assertEqual(ops.platform_label_offenders(labels, "demo"), [])

    def test_each_platform_prefix_is_refused(self):
        bad = {"traefik.http.routers.evil.rule": "Host(`evil.example`)", "ofelia.enabled": "true",
               "homepage.name": "x", "docker-volume-backup.stop-during-backup": "x"}
        self.assertEqual(ops.platform_label_offenders({**OURS, **bad}, "demo"), sorted(bad))

    def test_our_labels_with_other_values_or_other_keys_are_refused(self):
        f = ops.platform_label_offenders
        self.assertEqual(f({"com.kaldi.annex": "false"}, "demo"), ["com.kaldi.annex"])
        self.assertEqual(f({"com.kaldi.annex.app": "other"}, "demo"), ["com.kaldi.annex.app"])
        self.assertEqual(f({"com.kaldi.annex.owner": "sam"}, "demo"), ["com.kaldi.annex.owner"])
        self.assertEqual(f({"com.kaldi.annex.x": "1"}, "demo"), ["com.kaldi.annex.x"])


if __name__ == "__main__":
    unittest.main()
