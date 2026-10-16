"""annex_vm.py's user-account rules (D57). Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
