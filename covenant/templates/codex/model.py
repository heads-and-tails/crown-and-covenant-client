"""Codex provider adapter owned by this agent definition."""

from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import tomllib
import uuid
from covenant.transport import atomic_json


class ModelError(RuntimeError):
    def __init__(self, message, kind="model"):
        super().__init__(message)
        self.kind = kind


def classify(message):
    lower = message.lower()
    if any(
        s in lower for s in ("rate limit", "usage limit", "quota", "usage_limit", "429")
    ):
        return "account_limit"
    if any(
        s in lower
        for s in ("unauthorized", "authentication", "401", "sign in", "login")
    ):
        return "authentication"
    if any(
        s in lower for s in ("network", "connection", "stream disconnected", "timeout")
    ):
        return "model_network"
    return "model"


def restricted_config(root):
    cfg = {
        "web_search": "disabled",
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "project_doc_max_bytes": 0,
        "skills.include_instructions": False,
        "agents.enabled": False,
        "history.persistence": "none",
        "model_reasoning_effort": "medium",
        "model_auto_compact_token_limit": 48000,
        "include_apps_instructions": False,
        "model_provider": "openai",
    }
    for flag in (
        "shell_tool",
        "unified_exec",
        "apply_patch_freeform",
        "view_image",
        "apps",
        "connectors",
        "plugins",
        "remote_plugin",
        "recommended_plugins",
        "browser_use",
        "computer_use",
        "js_repl",
        "code_mode",
        "code_mode_only",
        "multi_agent",
        "multi_agent_v2",
        "collab",
        "goals",
        "memories",
        "memory_tool",
        "tool_search",
        "search_tool",
        "image_generation",
        "imagegenext",
        "hooks",
        "codex_hooks",
        "plugin_hooks",
        "request_permissions",
        "request_permissions_tool",
        "sleep_tool",
        "send_async_message",
        "in_app_local_automation",
        "workspace_dependencies",
    ):
        cfg["features." + flag] = False
    cfg["features.skip_host_skill_discovery"] = True
    # Explicitly disable inherited named MCP servers, not just an empty map merged with them.
    config_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        inherited = tomllib.loads((config_home / "config.toml").read_text())
        for name in inherited.get("mcp_servers", {}):
            cfg[f"mcp_servers.{name}.enabled"] = False
        for name in inherited.get("plugins", {}):
            cfg[f"plugins.{name}.enabled"] = False
    except (OSError, ValueError):
        pass
    cfg["mcp_servers"] = {}
    return cfg


class Model:
    def __init__(self, ctx):
        self.model = os.environ.get(
            "CODEX_MODEL", ctx.config.get("model", "gpt-5.6-luna")
        )
        self.timeout = max(30, min(300, ctx.config.get("timeout_seconds", 180)))
        self.root = ctx._runtime.private
        self.process = None
        self.messages = queue.Queue()
        self.sequence = 0
        self.thread_id = None
        self.cancelled = threading.Event()
        self.cycles = 0
        self.write_lock = threading.Lock()
        (self.root / "model").mkdir(exist_ok=True)

    @staticmethod
    def check_compatibility(root):
        executable = shutil.which("codex")
        if not executable:
            raise ModelError(
                "Install Codex CLI and run codex login before starting this agent.",
                "compatibility",
            )
        folder = Path(root) / "model" / "schema"
        folder.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                executable,
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(folder),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            schema = json.loads((folder / "v2" / "ThreadStartParams.json").read_text())
            assert all(k in schema["properties"] for k in ("dynamicTools", "ephemeral"))
            assert (folder / "DynamicToolCallResponse.json").exists()
        except (OSError, ValueError, AssertionError):
            raise ModelError(
                "This Codex CLI lacks the required experimental dynamic-tool interface. Upgrade Codex CLI and retry.",
                "compatibility",
            )
        if result.returncode:
            raise ModelError(
                "Codex App Server compatibility check failed. Upgrade Codex CLI and retry.",
                "compatibility",
            )
        return executable

    def _log(self, item):
        with (self.root / "model" / "transcript.jsonl").open(
            "a", encoding="utf-8"
        ) as f:
            f.write(json.dumps({"at": time.time(), **item}, ensure_ascii=False) + "\n")

    def _send(self, message):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise ModelError(
                    "Codex App Server stopped. Reconnecting.", "model_network"
                )
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def _request(self, method, params):
        self.sequence += 1
        key = self.sequence
        self._send({"id": key, "method": method, "params": params})
        return key

    def _receive(self, until):
        while time.monotonic() < until:
            if self.cancelled.is_set():
                raise ModelError(
                    "Model call interrupted by the watchdog.", "model_network"
                )
            try:
                return self.messages.get(
                    timeout=min(1, max(0.01, until - time.monotonic()))
                )
            except queue.Empty:
                if not self.process or self.process.poll() is not None:
                    raise ModelError(
                        "Codex App Server exited. Check the agent model/server.log.",
                        "model_network",
                    )
        raise ModelError(
            "Model response deadline exceeded. Pending conversations will retry.",
            "model_network",
        )

    def _response(self, key, timeout=30):
        until = time.monotonic() + timeout
        while True:
            m = self._receive(until)
            if m.get("id") == key and "method" not in m:
                if "error" in m:
                    raise ModelError(str(m["error"]), classify(str(m["error"])))
                return m.get("result", {})
            if "method" in m and "id" in m:
                self._send(
                    {
                        "id": m["id"],
                        "error": {
                            "code": -32601,
                            "message": "Only the assigned game tools are available.",
                        },
                    }
                )

    def _start(self):
        executable = self.check_compatibility(self.root)
        self.cancelled.clear()
        self.messages = queue.Queue()
        config = restricted_config(self.root)
        args = [executable, "app-server"]
        # Overrides also apply at server startup, before global integrations initialize.
        for key, value in config.items():
            args += ["-c", key + "=" + json.dumps(value, separators=(",", ":"))]
        self.stderr = (self.root / "model" / "server.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            args,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            bufsize=1,
            start_new_session=False,
        )
        process = self.process

        def read():
            try:
                for line in process.stdout:
                    try:
                        self.messages.put(json.loads(line))
                    except ValueError:
                        pass
            except (OSError, ValueError):
                pass

        threading.Thread(target=read, daemon=True, name="app-server-reader").start()
        key = self._request(
            "initialize",
            {
                "clientInfo": {"name": "covenant", "version": "0.4.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._response(key)
        self._send({"method": "initialized"})
        key = self._request(
            "thread/start",
            {
                "model": self.model,
                "cwd": str(self.root),
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "baseInstructions": self.instructions,
                "developerInstructions": "You may call only the provided game tools. Scripts must execute through the provided run_script Docker tool. Do not use built-in tools.",
                "dynamicTools": self.tools,
                "config": config,
            },
        )
        result = self._response(key)
        self.thread_id = result["thread"]["id"]
        self.cycles = 0
        atomic_json(
            self.root / "model" / "checkpoint.json",
            {
                "threadId": self.thread_id,
                "ephemeral": True,
                "model": self.model,
                "tools": [t["name"] for t in self.tools],
            },
        )

    def run(self, instructions, prompt, tools, execute):
        self.instructions = instructions
        self.tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["parameters"],
            }
            for t in tools
        ]
        try:
            if not self.process or self.process.poll() is not None or self.cycles >= 8:
                self.cancel()
                self._start()
            self._log({"kind": "input", "text": prompt})
            key = self._request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            until = time.monotonic() + self.timeout
            count = 0
            notes = []
            while True:
                m = self._receive(until)
                method = m.get("method")
                p = m.get("params", {})
                if m.get("id") == key and "error" in m:
                    raise ModelError(str(m["error"]))
                if method == "item/tool/call":
                    count += 1
                    if count > 40:
                        raise ModelError(
                            "Reasoning exceeded 40 tools; yielding to queued messages."
                        )
                    if p["tool"] not in {t["name"] for t in tools}:
                        result = {"ok": False, "error": "Unknown tool"}
                    else:
                        result = execute(p["tool"], p.get("arguments", {}))
                    self._log(
                        {
                            "kind": "tool",
                            "tool": p["tool"],
                            "arguments": p.get("arguments"),
                            "result": result,
                        }
                    )
                    self._send(
                        {
                            "id": m["id"],
                            "result": {
                                "contentItems": [
                                    {"type": "inputText", "text": json.dumps(result)}
                                ],
                                "success": not (
                                    isinstance(result, dict)
                                    and result.get("ok") is False
                                ),
                            },
                        }
                    )
                elif method and "id" in m:
                    self._send(
                        {
                            "id": m["id"],
                            "error": {
                                "code": -32601,
                                "message": "Only assigned game tools are available.",
                            },
                        }
                    )
                elif method == "item/completed":
                    item = p.get("item", {})
                    self._log({"kind": "item", "item": item})
                    if item.get("type") == "agentMessage":
                        notes.append(item.get("text", ""))
                    if item.get("type") in (
                        "commandExecution",
                        "fileChange",
                        "mcpToolCall",
                        "webSearch",
                    ):
                        raise ModelError(
                            "Unexpected built-in tool; stopping for isolation.",
                            "isolation",
                        )
                elif method == "turn/completed":
                    turn = p.get("turn", {})
                    if turn.get("status") != "completed":
                        raise ModelError(str(turn.get("error") or turn.get("status")))
                    break
                elif method == "error" and not p.get("willRetry"):
                    raise ModelError(str(p.get("error", p)))
            self.cycles += 1
            atomic_json(
                self.root / "model" / "last-cycle.json",
                {
                    "at": time.time(),
                    "tools": count,
                    "note": "\n".join(notes),
                    "success": True,
                },
            )
            return {"ok": True, "tool_calls": count}
        except Exception:
            self.cancel()
            raise

    def cancel(self):
        self.cancelled.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
        self.process = None
        self.thread_id = None
        if getattr(self, "stderr", None):
            self.stderr.close()
