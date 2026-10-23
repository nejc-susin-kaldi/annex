"""annex_vm.py's user-account rules (D57). Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402

USERS = {
    "sam": {"displayname": "Sam Novak", "email": "sam@annex.example", "password": "$argon2id$x", "groups": ["builders"]},
    "kaldi-admin": {"displayname": "Kaldi Admin", "email": "admin@annex.example", "password": "$argon2id$y",
                    "groups": ["admins", "builders"]},
    "old": {"displayname": "Old", "email": "old@annex.example", "password": "$argon2id$z", "groups": ["users"],
            "disabled": True},
}


class Validate(unittest.TestCase):
    def test_good(self):
        self.assertEqual(vm.validate_user("ana.k", "Ana Kovač", "ana@company.com", ["users"]), [])

    def test_bad_fields_say_how_to_fix(self):
        problems = vm.validate_user("Ana", "", "ana", ["Users", "ok"])
        self.assertEqual(len(problems), 4)
        self.assertTrue(all("e.g." in p or "use" in p for p in problems))

    def test_needs_a_group(self):
        self.assertIn("at least one", " ".join(vm.validate_user(groups=[])))

    def test_name_cannot_break_the_yaml_or_headers(self):
        self.assertTrue(vm.validate_user(name='Ana "x"'))
        self.assertTrue(vm.validate_user(name="Ana\nRemote-User: kaldi-admin"))


class Directory(unittest.TestCase):
    def test_no_hashes_no_disabled(self):
        d = vm.directory_entries(USERS)
        self.assertEqual(sorted(d), ["kaldi-admin", "sam"])
        self.assertNotIn("argon2", str(d))
        self.assertEqual(d["sam"], {"name": "Sam Novak", "email": "sam@annex.example", "groups": ["builders"]})

    def test_active_admins(self):
        self.assertEqual(vm.active_admins(USERS), ["kaldi-admin"])
        no_admin = {k: dict(v, disabled=True) if k == "kaldi-admin" else v for k, v in USERS.items()}
        self.assertEqual(vm.active_admins(no_admin), [])

    def test_public_user_has_no_password(self):
        self.assertNotIn("password", vm.public_user("sam", USERS["sam"]))


class DemoPasswords(unittest.TestCase):
    """secrets/users.txt (`login password` lines) is what the suites and metabase-setup.sh read (D107)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secrets = Path(self.tmp.name)
        patch = unittest.mock.patch.object(vm, "SECRETS", self.secrets)
        patch.start()
        self.addCleanup(patch.stop)
        self.path = self.secrets / "users.txt"

    def test_update_only_touches_an_existing_line(self):
        self.path.write_text("sam old\nkaldi-admin k\n")
        self.assertTrue(vm.update_demo_password("sam", "new"))
        self.assertEqual(self.path.read_text(), "sam new\nkaldi-admin k\n")
        self.assertFalse(vm.update_demo_password("maja", "x"))  # no line, add not asked for
        self.assertEqual(self.path.read_text(), "sam new\nkaldi-admin k\n")

    def test_no_file_and_no_add_does_nothing(self):
        self.assertFalse(vm.update_demo_password("sam", "x"))
        self.assertFalse(self.path.exists())

    def test_add_appends_a_new_login(self):
        self.path.write_text("sam s\n")
        self.assertTrue(vm.update_demo_password("maja", "m", add=True))
        self.assertEqual(self.path.read_text(), "sam s\nmaja m\n")

    def test_add_replaces_instead_of_duplicating(self):
        self.path.write_text("maja old\n")
        vm.update_demo_password("maja", "new", add=True)
        self.assertEqual(self.path.read_text(), "maja new\n")

    def test_add_creates_the_file_private(self):
        vm.update_demo_password("maja", "m", add=True)
        self.assertEqual(self.path.read_text(), "maja m\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([p.name for p in self.secrets.iterdir()], ["users.txt"])  # no temp file left

    def test_a_login_that_prefixes_another_is_not_confused(self):
        self.path.write_text("sam-b x\nsam y\n")
        vm.update_demo_password("sam", "z", add=True)
        self.assertEqual(self.path.read_text(), "sam-b x\nsam z\n")

    def test_drop(self):
        self.path.write_text("sam s\ncm c\n")
        self.assertTrue(vm.drop_demo_password("cm"))
        self.assertEqual(self.path.read_text(), "sam s\n")
        self.assertFalse(vm.drop_demo_password("cm"))
        self.assertFalse(vm.drop_demo_password("nobody"))

    def test_drop_without_a_file(self):
        self.assertFalse(vm.drop_demo_password("cm"))


class AddDemo(unittest.TestCase):
    """`users-add --demo`: random password printed once and kept in users.txt; never in the audit trail."""

    def run_add(self, **flags):
        a = argparse.Namespace(login="maja", actor="t", name="Maja Horvat", email="maja@goat-arcade.example",
                               groups="users", print_password=False, demo=False)
        for k, v in flags.items():
            setattr(a, k, v)
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(vm, "SECRETS", Path(d)), \
                unittest.mock.patch.object(vm, "users_lock", contextlib.nullcontext), \
                unittest.mock.patch.object(vm, "load_users", return_value=dict(USERS)), \
                unittest.mock.patch.object(vm, "save_users"), \
                unittest.mock.patch.object(vm, "random_password", return_value=("pw-123", "$argon2id$new")), \
                unittest.mock.patch.object(vm, "audit") as audit, \
                unittest.mock.patch.object(vm, "send_reset_email") as invite, \
                unittest.mock.patch.object(vm, "email_outside_domains", return_value=False), \
                contextlib.redirect_stdout(buf):
            vm.cmd_users_add(a)
            txt = Path(d) / "users.txt"
            stored = txt.read_text() if txt.exists() else None
        return json.loads(buf.getvalue()), stored, audit, invite

    def test_plain_add_invites_and_stores_nothing(self):
        res, stored, _, invite = self.run_add()
        self.assertIsNone(stored)
        self.assertNotIn("password", res)
        invite.assert_called_once()

    def test_print_password_alone_does_not_touch_users_txt(self):
        res, stored, _, _ = self.run_add(print_password=True)
        self.assertEqual(res["password"], "pw-123")
        self.assertIsNone(stored)

    def test_demo_prints_once_and_stores(self):
        res, stored, audit, invite = self.run_add(demo=True)
        self.assertEqual(res["password"], "pw-123")
        self.assertTrue(res["demo_password_stored"])
        self.assertEqual(stored, "maja pw-123\n")
        invite.assert_not_called()  # no invitation: a human hands the password over
        self.assertNotIn("pw-123", json.dumps(audit.call_args.args))


if __name__ == "__main__":
    unittest.main()
