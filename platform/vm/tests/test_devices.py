"""annex_vm.py's tailnet devices (ANNEX_ACCESS=headscale, D120): what a device row shows, the key email, the refusals,
and that a key reaches the mail server on stdin, never in an argument list. Stdlib unittest, runs on the Mac."""
import argparse
import contextlib
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402

NODE = {"id": 3, "name": "anas-macbook", "given_name": "anas-macbook", "user": {"id": 2, "name": "ana"},
        "ip_addresses": ["fd7a:115c:a1e0::3", "100.64.0.3"], "online": True,
        "last_seen": {"seconds": 1793283382, "nanos": 1}, "expiry": {"seconds": 0}}
SERVER = {"id": 1, "given_name": "annex", "user": {"id": 1, "name": "tagged-devices"}, "ip_addresses": ["100.64.0.1"],
          "tags": ["tag:annex-server"]}
SETTINGS = {"ANNEX_ACCESS": "headscale", "ANNEX_URL_VPN": "https://vpn.annex.kaldi.si:8443",
            "ANNEX_URL_HOME": "https://home.annex.kaldi.si"}
USERS = {"ana": {"displayname": "Ana Kovač", "email": "ana@kaldi.si", "groups": ["users"]},
         "old": {"displayname": "Old", "email": "old@kaldi.si", "groups": ["users"], "disabled": True}}
KEY = "hskey-auth-secretsecret"


def run_cmd(fn, **args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(argparse.Namespace(actor="nejc", **args))
    return json.loads(buf.getvalue())


class Rows(unittest.TestCase):
    def test_device_row(self):
        self.assertEqual(vm.device_row(NODE), {"id": 3, "login": "ana", "device": "anas-macbook", "ip": "100.64.0.3",
                                               "online": True, "last_seen": "2026-10-29T14:16:22Z", "expires": None,
                                               "tags": []})
        self.assertEqual(vm.device_row(SERVER)["tags"], ["tag:annex-server"])
        self.assertEqual(vm.device_row({})["ip"], "")

    def test_email_says_what_to_do(self):
        m = vm.device_key_email("Ana", "ana@kaldi.si", SETTINGS["ANNEX_URL_VPN"], SETTINGS["ANNEX_URL_HOME"], KEY,
                                "2026-10-30T12:00:00Z")
        self.assertEqual(m["to"], "ana@kaldi.si")
        for part in (KEY, "--login-server https://vpn.annex.kaldi.si:8443", "tailscale.com/download",
                     "https://home.annex.kaldi.si", "2026-10-30T12:00:00Z"):
            self.assertIn(part, m["text"])
        self.assertNotIn("expires", vm.device_key_email("A", "a@b.c", "u", "h", KEY, None)["text"])


class Commands(unittest.TestCase):
    def setUp(self):
        patches = [unittest.mock.patch.object(vm, "platform_setting", lambda k: SETTINGS.get(k, "")),
                   unittest.mock.patch.object(vm, "load_users", lambda: USERS),
                   unittest.mock.patch.object(vm, "audit")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_tailnet(self):
        with unittest.mock.patch.object(vm, "platform_setting", lambda k: "open"), \
                self.assertRaisesRegex(vm.Fail, "ANNEX_ACCESS=headscale"):
            vm.headscale("nodes", "list")

    def test_invite_emails_the_key_on_stdin(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get("input")))
            args = cmd[4:-2] if cmd[2] == vm.HEADSCALE else []
            if args[:2] == ["users", "list"]:
                out = []
            elif args[:2] == ["users", "create"]:
                out = {"id": 7, "name": "ana"}
            elif args[:2] == ["preauthkeys", "create"]:
                self.assertEqual(args[2:], ["--user", "7", "--expiration", "24h"])
                out = {"key": KEY, "expiration": {"seconds": 1793369783}}
            else:
                out = None
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out) if out is not None else "", stderr="")

        with unittest.mock.patch.object(vm, "run", fake_run):
            res = run_cmd(vm.cmd_users_device_invite, login="ana", hours=24, print_key=False)
        self.assertEqual((res["sent_to"], res["expires"]), ("ana@kaldi.si", "2026-10-30T14:16:23Z"))
        self.assertNotIn("key", res)
        send = [c for c in calls if vm.OPS in c[0]]
        self.assertEqual(len(send), 1)
        self.assertNotIn(KEY, " ".join(send[0][0]))       # not in the process list
        self.assertIn(KEY, json.loads(send[0][1])["text"])  # in the message, on stdin

    def test_invite_refusals(self):
        for login, hours, why in (("nobody", 24, "no user"), ("old", 24, "disabled"), ("ana", 0, "--hours"),
                                  ("ana", 500, "--hours")):
            with self.subTest(login=login, hours=hours), self.assertRaisesRegex(vm.Fail, why):
                vm.cmd_users_device_invite(argparse.Namespace(actor="x", login=login, hours=hours, print_key=False))

    def test_remove_one_device_or_all(self):
        deleted = []

        def fake_headscale(*args):
            if args[:2] == ("nodes", "list"):
                return [NODE, SERVER]
            if args[:2] == ("users", "list"):
                return [{"id": 2, "name": "ana"}]
            deleted.append(args[:2] + (args[3],))
            return None

        with unittest.mock.patch.object(vm, "headscale", fake_headscale):
            with self.assertRaisesRegex(vm.Fail, "no device 1"):
                vm.remove_devices("ana", 1)  # the server is not Ana's
            self.assertEqual([r["id"] for r in vm.remove_devices("ana", 3)], [3])
            self.assertEqual(deleted, [("nodes", "delete", "3")])
            deleted.clear()
            vm.remove_devices("ana")
            self.assertEqual(deleted, [("nodes", "delete", "3"), ("users", "destroy", "2")])


if __name__ == "__main__":
    unittest.main()
