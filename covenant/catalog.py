"""Read manifests and pin complete local agent definitions without importing code."""

from dataclasses import dataclass
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tomllib
from .transport import atomic_json

EXCLUDE = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "instances",
    "node_modules",
    ".covenant",
}


def files(root):
    for p in sorted(root.rglob("*")):
        relative = p.relative_to(root)
        if any(part in EXCLUDE or part.startswith(".env") for part in relative.parts):
            continue
        if p.is_symlink():
            raise ValueError("Agent definitions may not contain symlinks.")
        if p.is_file():
            yield p


@dataclass
class Definition:
    root: Path
    manifest: dict
    public: dict


def discover(directory):
    base = Path(directory).resolve()
    if not base.is_dir():
        return []
    result = []
    for root in sorted(base.iterdir()):
        if not root.is_dir() or root.is_symlink() or root.name.startswith("."):
            continue
        try:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", root.name):
                raise ValueError(
                    "Folder names need letters, numbers, underscores or hyphens."
                )
            manifest = tomllib.loads((root / "agent.toml").read_text())
            entry = manifest.get("entrypoint", "agent:MyAgent")
            if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*", entry):
                raise ValueError("Use module:ClassName for entrypoint.")
            source = root / (entry.split(":")[0].replace(".", "/") + ".py")
            if not source.is_file():
                raise ValueError("Entrypoint Python file is missing.")
            digest = hashlib.sha256()
            for p in files(root):
                content = p.read_bytes()
                if len(content) > 4_000_000:
                    raise ValueError(
                        "Definition files must be under 4 MB; put run data in instances."
                    )
                if p.suffix == ".py":
                    ast.parse(content, filename=str(p))
                digest.update(
                    str(p.relative_to(root)).encode() + b"\0" + content + b"\0"
                )
            for field in ("required_env", "required_commands"):
                if not isinstance(manifest.get(field, []), list) or not all(
                    isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v)
                    for v in manifest.get(field, [])
                ):
                    raise ValueError(field + " must be a list of names.")
            missing = [
                name
                for name in manifest.get("required_env", [])
                if not os.environ.get(name)
            ]
            missing += [
                name + " executable"
                for name in manifest.get("required_commands", [])
                if not shutil.which(name)
            ]
            info = {
                "id": root.name,
                "name": str(manifest.get("name", root.name))[:64],
                "description": str(manifest.get("description", ""))[:240],
                "version": digest.hexdigest(),
                "available": not missing,
            }
            if missing:
                info["error"] = "Configure locally: " + ", ".join(missing)
            result.append(Definition(root, manifest, info))
        except (ValueError, OSError, SyntaxError) as e:
            result.append(
                Definition(
                    root,
                    {},
                    {
                        "id": re.sub("[^a-zA-Z0-9_-]", "_", root.name)[:80],
                        "name": root.name[:64],
                        "description": "",
                        "version": "invalid",
                        "available": False,
                        "error": str(e).splitlines()[0][:240],
                    },
                )
            )
    return result


def pin(definition, destination):
    destination = Path(destination)
    stamp = destination / "pinned.json"
    if stamp.exists():
        old = json.loads(stamp.read_text())
        if old["version"] != definition.public["version"]:
            raise ValueError("This instance is pinned to another agent version.")
        return destination / "definition"
    directory = destination / "definition"
    directory.mkdir(parents=True, exist_ok=True)
    for p in files(definition.root):
        target = directory / p.relative_to(definition.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
    atomic_json(stamp, definition.public)
    return directory


def initialize(root):
    target = Path(root).resolve() / "agents"
    templates = Path(__file__).parent / "templates"
    target.mkdir(parents=True, exist_ok=True)
    for template in templates.iterdir():
        if template.is_dir() and not (target / template.name).exists():
            shutil.copytree(
                template,
                target / template.name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    return target
