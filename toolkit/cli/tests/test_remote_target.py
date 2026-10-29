"""Remote's target (D99): settings from the file ANNEX_ENV selects, or without it the instance `annex connect` pointed
at (D119); the local target is only another command prefix."""
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from annex_cli import remote
from annex_cli.remote import AnnexError, Remote, target_settings

LOCAL_FILE = """# local
ANNEX_TARGET=local
ANNEX_DOCKER_CONTEXT=desktop-linux
ANNEX_SSH_HOST=annex
ANNEX_DATA_DIR=$HOME/.annex/local
"""


VM = {"ANNEX_ENV": "vm"}  # the repo's .env lane, as scripts/lib.sh sets it


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        p = unittest.mock.patch.object(remote, "REPO", self.repo)
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, text):
        (self.repo / name).write_text(text)


class TargetSettings(Repo):
    def test_vm_defaults_without_any_file(self):
        t = target_settings(VM, self.repo)
        self.assertEqual((t["ANNEX_ENV"], t["ANNEX_TARGET"], t["ANNEX_SSH_HOST"], t["ANNEX_DATA_DIR"]),
                         ("vm", "ssh", "annex", "/srv/annex"))

    def test_vm_reads_env_then_example(self):
        self.write(".env.example", "ANNEX_SSH_HOST=from-example\n")
        self.assertEqual(target_settings(VM, self.repo)["ANNEX_SSH_HOST"], "from-example")
        self.write(".env", "ANNEX_SSH_HOST=from-env\nANNEX_DATA_DIR=/srv/x\n")
        t = target_settings(VM, self.repo)
        self.assertEqual((t["ANNEX_SSH_HOST"], t["ANNEX_DATA_DIR"]), ("from-env", "/srv/x"))

    def test_file_wins_and_environment_fills_gaps(self):
        self.write(".env", "ANNEX_SSH_HOST=file-host\n")
        t = target_settings({**VM, "ANNEX_SSH_HOST": "shell-host", "ANNEX_DATA_DIR": "/shell/data"}, self.repo)
        self.assertEqual((t["ANNEX_SSH_HOST"], t["ANNEX_DATA_DIR"]), ("file-host", "/shell/data"))

    def test_target_never_from_the_shell(self):
        # a shell left with the local stack's variables must not send a VM command to the Mac's Docker
        t = target_settings({**VM, "ANNEX_TARGET": "local", "ANNEX_DOCKER_CONTEXT": "desktop-linux"}, self.repo)
        self.assertEqual((t["ANNEX_TARGET"], t["ANNEX_DOCKER_CONTEXT"]), ("ssh", ""))

    def test_local_reads_env_local_and_expands_home(self):
        self.write(".env.local.example", LOCAL_FILE)
        with unittest.mock.patch.dict("os.environ", {"HOME": "/Users/someone"}):
            t = target_settings({"ANNEX_ENV": "local"}, self.repo)
        self.assertEqual((t["ANNEX_ENV"], t["ANNEX_TARGET"], t["ANNEX_DOCKER_CONTEXT"]), ("local", "local", "desktop-linux"))
        self.assertEqual(t["ANNEX_DATA_DIR"], "/Users/someone/.annex/local")

    def test_an_instance_reads_its_own_file(self):
        # another instance (D118): .env.<name>, never .env or an example
        self.write(".env", "ANNEX_SSH_HOST=homelab\n")
        with self.assertRaises(AnnexError) as e:
            target_settings({"ANNEX_ENV": "kaldi"}, self.repo)
        self.assertIn(".env.kaldi", str(e.exception))
        self.write(".env.kaldi", "ANNEX_SSH_HOST=annex-kaldi\n")
        t = target_settings({"ANNEX_ENV": "kaldi"}, self.repo)
        self.assertEqual((t["ANNEX_ENV"], t["ANNEX_TARGET"], t["ANNEX_SSH_HOST"]), ("kaldi", "ssh", "annex-kaldi"))

    def test_bad_values(self):
        for bad in ("prod", "Prod", "../x"):
            with self.subTest(bad=bad), self.assertRaises(AnnexError):
                target_settings({"ANNEX_ENV": bad}, self.repo)
        self.write(".env", "ANNEX_TARGET=cloud\n")
        with self.assertRaises(AnnexError):
            target_settings(VM, self.repo)


class Pointer(Repo):
    """Without ANNEX_ENV the CLI goes where `annex connect` pointed this user (D119), never to the repo's .env."""
    def env(self, **kw):
        return {"ANNEX_INSTANCE_FILE": str(self.repo / "instance"), **kw}

    def test_not_connected_says_what_to_run(self):
        self.write(".env", "ANNEX_SSH_HOST=homelab\n")  # a checkout's .env is no fallback
        with self.assertRaises(AnnexError) as e:
            target_settings(self.env(), self.repo)
        self.assertIn("annex connect <ssh-host>", str(e.exception))

    def test_pointer_wins_and_the_shell_cannot_redirect_it(self):
        self.write("instance", "# by annex connect\nANNEX_SSH_HOST=annex-kaldi\nANNEX_DATA_DIR=/srv/annex\n")
        t = target_settings(self.env(ANNEX_TARGET="local", ANNEX_SSH_HOST="other", ANNEX_DOCKER_CONTEXT="x"), self.repo)
        self.assertEqual((t["ANNEX_ENV"], t["ANNEX_TARGET"], t["ANNEX_SSH_HOST"], t["ANNEX_DATA_DIR"], t["ANNEX_DOCKER_CONTEXT"]),
                         ("", "ssh", "annex-kaldi", "/srv/annex", ""))
        r = Remote(self.env())
        self.assertEqual((r.env_name, r.host, r.local), ("", "annex-kaldi", False))

    def test_annex_env_still_selects_a_lane(self):
        self.write("instance", "ANNEX_SSH_HOST=annex-kaldi\n")
        self.write(".env", "ANNEX_SSH_HOST=homelab\n")
        self.assertEqual(target_settings(self.env(ANNEX_ENV="vm"), self.repo)["ANNEX_SSH_HOST"], "homelab")

    def test_instance_file_location(self):
        self.assertEqual(remote.instance_file({"XDG_CONFIG_HOME": "/x"}), Path("/x/annex/instance"))
        self.assertEqual(remote.instance_file({"ANNEX_INSTANCE_FILE": "/t/i"}), Path("/t/i"))
        self.assertEqual(remote.instance_file({}), Path.home() / ".config" / "annex" / "instance")


class Argv(Repo):
    def test_ssh_prefix_unchanged(self):
        r = Remote(VM)
        self.assertFalse(r.local)
        argv = r._argv("echo 'hi'")
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "annex")
        self.assertEqual(argv[-1], "bash -c 'echo '\"'\"'hi'\"'\"''")
        self.assertEqual(r._argv("x", tty=True)[-2:], ["-t", "bash -c x"])
        self.assertEqual(r.hint("docker ps"), "ssh annex docker ps")

    def local(self, context="desktop-linux", endpoint="unix:///Users/someone/.docker/run/docker.sock"):
        self.write(".env.local", LOCAL_FILE.replace("desktop-linux", context))
        with unittest.mock.patch.dict("os.environ", {"HOME": "/Users/someone"}):
            r = Remote({"ANNEX_ENV": "local", "PATH": "/usr/bin:/bin"})
        p = unittest.mock.patch.object(remote, "docker_endpoint", return_value=endpoint)
        p.start()
        self.addCleanup(p.stop)
        return r

    def test_local_prefix(self):
        r = self.local()
        self.assertTrue(r.local)
        self.assertEqual(r.data, "/Users/someone/.annex/local")
        argv = r._argv("echo 'hi'")
        self.assertEqual(argv[-3:], ["bash", "-c", "echo 'hi'"])  # the script itself, unquoted: no ssh in between
        self.assertEqual(argv[0], "env")
        self.assertIn("ANNEX_DATA_DIR=/Users/someone/.annex/local", argv)
        self.assertEqual(argv[1:4], ["-u", "DOCKER_HOST", "DOCKER_CONTEXT=desktop-linux"])
        path = next(a for a in argv if a.startswith("PATH="))
        self.assertTrue(path.startswith(f"PATH={Path(sys.executable).parent}:"), path)
        self.assertEqual(r._argv("x", tty=True)[-1], "x")
        self.assertEqual(r.hint("docker ps"), "docker ps")
        self.assertIn("local stack", r.where)

    def test_local_runs_the_script_here(self):
        r = self.local()
        with unittest.mock.patch.object(remote.subprocess, "run",
                                        return_value=subprocess.CompletedProcess([], 0, b"ok\n", b"")) as run:
            self.assertEqual(r.out("echo ok"), "ok\n")
        self.assertEqual(run.call_args.args[0][-1], "echo ok")

    def test_local_really_executes(self):
        # the prefix is a working command line on this machine (env + bash), with the data dir in the environment
        r = self.local()
        self.assertEqual(r.out("echo $ANNEX_DATA_DIR").strip(), "/Users/someone/.annex/local")
        self.assertEqual(r.run("exit 3", check=False).returncode, 3)

    def test_call_streams_through_the_local_prefix(self):
        # V1's Remote.call (D101) builds its command with vm_command and _argv: locally that is a real local process
        r = self.local()
        r.data = self.tmp.name
        vmdir = Path(self.tmp.name) / "platform" / "vm"
        vmdir.mkdir(parents=True)
        (vmdir / "annex_vm.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'progress': 'step one'}), flush=True)\n"
            "print(json.dumps({'ok': True, 'args': sys.argv[1:], 'stdin': sys.stdin.read(),"
            " 'exe': sys.executable, 'data': os.environ['ANNEX_DATA_DIR']}))\n")
        seen = []
        res = r.call("deploy", "demo", input="tar-bytes", progress=seen.append)
        self.assertEqual(seen, ["step one"])
        self.assertEqual((res["args"], res["stdin"]), (["deploy", "demo"], "tar-bytes"))
        # python3 on the prefix's PATH is this CLI's interpreter (PyYAML), not whatever the system has
        self.assertEqual(Path(res["exe"]).parent, Path(sys.executable).parent)
        self.assertEqual(r.vm_command("x"), f"python3 {self.tmp.name}/platform/vm/annex_vm.py x")

    def test_context_that_reaches_the_vm_is_refused(self):
        r = self.local(context="annex-vm", endpoint="ssh://annex")
        with self.assertRaises(AnnexError) as e:
            r._argv("docker ps")
        self.assertIn("not a local socket", str(e.exception))

    def test_no_context_leaves_docker_alone(self):
        r = self.local(context="")
        argv = r._argv("x")
        self.assertNotIn("-u", argv)
        self.assertFalse(any(a.startswith("DOCKER_CONTEXT=") for a in argv))

    def test_settings_error_names_the_local_commands(self):
        r = self.local()
        with unittest.mock.patch.object(remote.subprocess, "run",
                                        return_value=subprocess.CompletedProcess([], 1, b"", b"no such file")):
            with self.assertRaises(AnnexError) as e:
                r.settings
        self.assertIn("ANNEX_ENV=local scripts/bootstrap.sh", str(e.exception))


if __name__ == "__main__":
    unittest.main()
