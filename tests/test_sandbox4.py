"""Explicit Docker tests: COVENANT_DOCKER_TESTS=1 python -m unittest discover -s tests -p test_sandbox4.py."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from covenant import Runtime
from covenant.sandbox import Sandbox
from test_sdk4 import Fake, Idle


@unittest.skipUnless(
    os.environ.get("COVENANT_DOCKER_TESTS") == "1", "Requires Docker daemon"
)
class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Runtime(Fake(), Idle(), self.tmp.name, command_timeout=0)
        self.r._accept(self.r.client.o)
        self.box = Sandbox(self.r.ctx)

    def tearDown(self):
        self.box.close()
        self.r.close()
        self.tmp.cleanup()

    def script(self, text):
        p = self.r.workspace / "test.py"
        p.write_text(text)
        return p

    def test_state_and_preview_commands(self):
        self.script(
            "from game import ctx\ns=ctx.get_state()\nassert s.turn==1\nprint(ctx.hold(army=s.get_army('a1')).status)\n"
        )
        r = self.box.run("test.py", preview=True)
        self.assertTrue(r["ok"], r)
        self.assertIn("preview", r["output"])
        self.assertEqual(self.r.journal.outgoing(), [])
        r = self.box.run("test.py")
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.r.journal.outgoing()), 1)

    def test_syntax_error_timeout_repair_and_isolation(self):
        self.script("this is invalid python!")
        r = self.box.run("test.py")
        self.assertFalse(r["ok"])
        self.assertIn("SyntaxError", r["output"])
        self.script("while True: pass")
        r = self.box.run("test.py", timeout=1)
        self.assertTrue(r["timed_out"])
        self.script(
            "import os,socket,pathlib\nassert 'OPENAI_API_KEY' not in os.environ\nassert not pathlib.Path('/var/run/docker.sock').exists()\nassert not pathlib.Path('/home/ivan/.codex/auth.json').exists()\nassert not pathlib.Path('/workspace/../runtime/connection.json').exists()\ns=socket.socket();s.settimeout(.2)\ntry:s.connect(('1.1.1.1',443));raise AssertionError('network escaped')\nexcept OSError:pass\nfrom game import ctx\ntry:ctx._request('/other-player');raise AssertionError('scope escaped')\nexcept RuntimeError:pass\nprint('isolated and repaired')\n"
        )
        r = self.box.run("test.py")
        self.assertTrue(r["ok"], r)
        self.assertIn("isolated and repaired", r["output"])

    def test_self_modifying_scripts_need_new_validation(self):
        self.script(
            "from pathlib import Path\nPath(__file__).write_text('print(123)')\n"
        )
        result = self.box.run("test.py", preview=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["source_unchanged"])
        with self.assertRaisesRegex(ValueError, "changed after validation"):
            self.box.run("test.py", expected_hash=result["source_hash"])

    def test_supervisor_removes_crashed_instances_container(self):
        import subprocess
        from covenant.supervisor import Instance

        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.box.name,
                "--network",
                "none",
                "python:3.13-slim",
                "sleep",
                "30",
            ],
            capture_output=True,
            check=True,
        )
        instance = Instance.__new__(Instance)
        instance.process = None
        instance.job = {"instanceId": self.r.instance_id}
        instance.cleanup_execution()
        result = subprocess.run(
            ["docker", "inspect", self.box.name], capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)

    def test_paths_cannot_escape(self):
        for path in ("../runtime/connection.json", "/etc/passwd", ".git/config"):
            with self.assertRaises(ValueError):
                self.box.path(path)
        (self.r.workspace / "escape").symlink_to(
            self.r.private, target_is_directory=True
        )
        with self.assertRaises(ValueError):
            self.box.path("escape/connection.json")


if __name__ == "__main__":
    unittest.main()
