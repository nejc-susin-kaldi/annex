"""env: ANNEX_CSRF may be set (to turn off the runtime's cross-site check); other ANNEX_ names may not (hardening A4)."""
import tempfile
import unittest
from pathlib import Path

try:
    from annex_cli import manifest as mf
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


def problems(env_line: str) -> list[str]:
    d = Path(tempfile.mkdtemp())
    (d / "annex.yaml").write_text("name: demo-app\nowner: sam\ntype: job\nschedule:\n  - {name: nightly, cron: '0 6 * * *', command: 'true'}\n"
                                  f"env:\n  {env_line}\n")
    try:
        mf.load(d)
    except mf.ManifestError as e:
        return [x for x in e.problems if x.startswith("env.")]  # other problems (no Dockerfile here) don't matter
    return []


class CsrfEnv(unittest.TestCase):
    def test_off_is_allowed_quoted_or_not(self):
        self.assertEqual(problems('ANNEX_CSRF: "off"'), [])
        self.assertEqual(problems("ANNEX_CSRF: off"), [])  # YAML reads it as False
        self.assertEqual(problems('ANNEX_CSRF: "on"'), [])

    def test_other_values_say_what_to_use(self):
        out = problems('ANNEX_CSRF: "maybe"')
        self.assertEqual(len(out), 1)
        self.assertIn("on, off", out[0])
        self.assertIn("default is on", out[0])

    def test_other_annex_names_stay_reserved(self):
        self.assertIn("set by Annex", problems("ANNEX_OPS_TOKEN: x")[0])

    def test_log_level_message_unchanged(self):
        self.assertIn("the default is info", problems("LOG_LEVEL: loud")[0])


if __name__ == "__main__":
    unittest.main()
