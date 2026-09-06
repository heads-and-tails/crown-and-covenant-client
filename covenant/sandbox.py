"""Optional Docker execution for model-written scripts, scoped to one instance."""

import json
import inspect
import hashlib
import os
from pathlib import Path
import socketserver
import subprocess
import threading
import time
from .context import Context
from .state import Army, Structure, Record, RouteOrder, ProductionOrder, thaw

BRIDGE = r"""
import json,socket
from state import GameState,Army,Structure,Record,RouteOrder,ProductionOrder,thaw
class RemoteContext:
    workspace=__import__('pathlib').Path('/workspace')
    def __getattr__(self,name):
        def call(*args,**kwargs):
            def encode(a):
                if isinstance(a,(list,tuple)) and a and isinstance(a[0],(RouteOrder,ProductionOrder)):
                    a=[{'kind':'route','army':thaw(o.army),'steps':thaw(o.steps)} if isinstance(o,RouteOrder) else {'kind':'production','castle':thaw(o.castle),'troop':o.troop,'quantity':o.quantity} for o in a]
                return thaw(a)
            encoded=[encode(a) for a in args]
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
                s.connect('/bridge/game.sock');s.sendall((json.dumps({'method':name,'args':encoded,'kwargs':{k:encode(v) for k,v in kwargs.items()}})+'\n').encode())
                f=s.makefile('r');r=json.loads(f.readline())
            if 'error' in r:raise RuntimeError(r['error'])
            if name in ('get_conversation','find_path'):return tuple(Record(v) for v in r['result'])
            return GameState(r['result']) if name=='get_state' else Record(r['result']) if isinstance(r['result'],dict) else r['result']
        return call
ctx=RemoteContext()
"""


class Sandbox:
    def __init__(self, ctx, image="python:3.13-slim"):
        self.ctx = ctx
        self.root = ctx.workspace.resolve()
        self.private = ctx._runtime.private / "script-bridge"
        self.private.mkdir(parents=True, exist_ok=True)
        self.image = image
        self.lock = threading.Lock()
        self.preview = False
        self.name = "covenant-script-" + ctx.instance_id[:32]
        (self.private / "game.py").write_text(BRIDGE)
        (self.private / "state.py").write_text(
            (Path(__file__).parent / "state.py").read_text()
        )
        self.socket = self.private / "game.sock"
        self.socket.unlink(missing_ok=True)
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                raw = self.rfile.readline(262145)
                if len(raw) > 262144:
                    return
                try:
                    request = json.loads(raw)
                    result = owner.dispatch(request)
                    response = {"result": thaw(result)}
                except Exception as e:
                    response = {"error": str(e)[:500]}
                self.wfile.write((json.dumps(response) + "\n").encode())

        # Unix socket addresses are capped at 108 bytes, while instance paths can be much longer.
        self.directory_fd = os.open(self.private, os.O_RDONLY | os.O_DIRECTORY)
        address = (
            f"/proc/self/fd/{self.directory_fd}/game.sock"
            if Path("/proc/self/fd").exists()
            else str(self.socket)
        )
        self.server = socketserver.ThreadingUnixStreamServer(address, Handler)
        self.server.daemon_threads = True
        os.chmod(self.socket, 0o600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def dispatch(self, r):
        name = r["method"]
        args = r.get("args", [])
        kwargs = r.get("kwargs", {})
        queries = {"get_state", "get_conversation", "get_receipt", "find_path"}
        commands = {
            "move",
            "set_route",
            "hold",
            "recruit",
            "submit_orders",
            "send_message",
            "reply",
            "propose_trade",
            "respond_trade",
            "set_ready",
        }
        if name not in queries | commands:
            raise ValueError(
                "Only this instance’s documented game methods are available."
            )
        method = getattr(self.ctx, name)
        bound = inspect.signature(method).bind(*args, **kwargs)
        if name in {"move", "set_route", "hold", "find_path"}:
            bound.arguments["army"] = Army(bound.arguments["army"])
        if name == "recruit":
            bound.arguments["castle"] = Structure(bound.arguments["castle"])
        if name == "submit_orders":
            bound.arguments["orders"] = [
                (
                    RouteOrder(Army(o["army"]), o["steps"])
                    if o["kind"] == "route"
                    else ProductionOrder(
                        Structure(o["castle"]), o["troop"], o.get("quantity", 1)
                    )
                )
                for o in bound.arguments["orders"]
            ]
        if self.preview and name in commands:
            return {
                "ok": True,
                "status": "preview",
                "method": name,
                "detail": "No command sent during validation.",
            }
        return method(*bound.args, **bound.kwargs)

    def path(self, name):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("Use a relative path within this workspace, outside .git.")
        path = (self.root / relative).resolve()
        if path == self.root or not path.is_relative_to(self.root):
            raise ValueError("Path escapes the instance workspace.")
        return path

    def source_digest(self, name):
        try:
            path = self.path(name)
            return (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
        except (ValueError, OSError):
            return None

    def run(self, path, preview=False, timeout=15, expected_hash=None):
        script = self.path(path)
        if script.suffix != ".py" or not script.is_file():
            raise ValueError("Choose a Python script in this workspace.")
        if not self.lock.acquire(blocking=False):
            raise ValueError("A script is already running in this instance.")
        self.preview = preview
        output = []
        total = 0
        try:
            source_hash = hashlib.sha256(script.read_bytes()).hexdigest()
            if expected_hash and source_hash != expected_hash:
                raise ValueError(
                    "The script changed after validation; preview it again."
                )
            subprocess.run(
                ["docker", "rm", "-f", self.name], capture_output=True, timeout=10
            )
            args = [
                "docker",
                "run",
                "--name",
                self.name,
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=32m",
                "--log-driver",
                "none",
                "--mount",
                f"type=bind,src={self.root},dst=/workspace",
                "--mount",
                f"type=bind,src={self.private},dst=/bridge,readonly",
                "--workdir",
                "/workspace",
                "--env",
                "PYTHONPATH=/bridge",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                self.image,
                "python",
                "-u",
                "/workspace/" + str(script.relative_to(self.root)),
            ]
            process = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            def read():
                nonlocal total
                while chunk := process.stdout.read(4096):
                    if total < 32000:
                        output.append(chunk[: 32000 - total])
                        total += len(chunk)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            timed_out = False
            try:
                process.wait(timeout=max(1, min(timeout, 30)))
            except subprocess.TimeoutExpired:
                timed_out = True
                subprocess.run(
                    ["docker", "rm", "-f", self.name], capture_output=True, timeout=10
                )
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            return {
                "ok": process.returncode == 0 and not timed_out,
                "source_hash": source_hash,
                "source_unchanged": self.source_digest(path) == source_hash,
                "preview": preview,
                "exit_code": process.returncode,
                "timed_out": timed_out,
                "output": b"".join(output).decode(errors="replace"),
            }
        finally:
            subprocess.run(
                ["docker", "rm", "-f", self.name], capture_output=True, timeout=10
            )
            self.preview = False
            self.lock.release()

    def close(self):
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, timeout=10
        )
        self.server.shutdown()
        self.server.server_close()
        self.socket.unlink(missing_ok=True)
        os.close(self.directory_fd)
