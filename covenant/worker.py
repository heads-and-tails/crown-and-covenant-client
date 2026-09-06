"""Private process entrypoint; imports only the assigned pinned definition."""

import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib
from .runtime import Runtime
from .transport import Connection, Client, atomic_json


def main():
    root = Path(sys.argv[1]).resolve()
    private = root / "runtime"
    definition = private / "definition"
    manifest = tomllib.loads((definition / "agent.toml").read_text())
    os.chdir(definition)
    sys.path.insert(0, str(definition))
    module, name = manifest.get("entrypoint", "agent:MyAgent").split(":")
    assignment = json.loads((private / "assignment.json").read_text())
    runtime = None
    try:
        requirements = definition / "requirements.txt"
        if requirements.exists() and any(
            line.strip() and not line.lstrip().startswith("#")
            for line in requirements.read_text().splitlines()
        ):
            # Each pinned definition gets its own dependency directory; other instances never mutate it.
            deps = private / "dependencies"
            if not (deps / ".complete").exists():
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--target",
                        str(deps),
                        "-r",
                        str(requirements),
                    ],
                    check=True,
                    timeout=180,
                )
                (deps / ".complete").touch()
            sys.path.insert(0, str(deps))
        agent = getattr(importlib.import_module(module), name)()
        runtime = Runtime(
            Client(Connection.load(private / "connection.json")),
            agent,
            root,
            assignment["instanceId"],
            {
                **manifest.get("config", {}),
                "fast": "--fast" in sys.argv
                or manifest.get("config", {}).get("fast", False),
            },
            poll=max(0.1, min(30, manifest.get("config", {}).get("poll_seconds", 2))),
        )

        def stop():
            sys.stdin.readline()
            runtime.stop.set()
            runtime.wake.set()

        threading.Thread(target=stop, daemon=True).start()
        runtime.run()
    except Exception as e:
        logging.exception("Instance failed")
        atomic_json(
            private / "status.json",
            {
                "status": "offline",
                "at": __import__("time").time(),
                "error": {"kind": "worker", "message": str(e)[:400]},
            },
        )
        return 1
    finally:
        if runtime:
            runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
