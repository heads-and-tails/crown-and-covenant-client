"""Responses API adapter. API billing/configuration belong to this definition."""

import json
import os
import threading
import time
import urllib.request
import urllib.error


class Model:
    def __init__(self, ctx):
        self.key = os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise RuntimeError(
                "Authentication: set OPENAI_API_KEY before launching the client."
            )
        self.model = os.environ.get("OPENAI_MODEL", ctx.config.get("model", "gpt-5.5"))
        self.root = ctx._runtime.private
        self.history = []
        self.cycles = 0
        self.cancelled = threading.Event()
        (self.root / "model").mkdir(exist_ok=True)

    def cancel(self):
        self.cancelled.set()

    def run(self, instructions, prompt, tools, execute):
        self.cancelled.clear()
        if self.cycles >= 8:
            self.history = []
            self.cycles = 0
        self.history.append({"role": "user", "content": prompt})
        calls = 0
        deadline = time.monotonic() + 120
        while not self.cancelled.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Model response deadline exceeded; pending messages will retry."
                )
            data = {
                "model": self.model,
                "instructions": instructions,
                "input": self.history,
                "tools": [{"type": "function", **t, "strict": True} for t in tools],
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(data).encode(),
                headers={
                    "Authorization": "Bearer " + self.key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=max(1, min(60, deadline - time.monotonic()))
                ) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"OpenAI API {e.code}: " + e.read(2048).decode(errors="replace")
                ) from None
            if self.cancelled.is_set():
                raise RuntimeError("Model call stopped.")
            if result.get("status") not in (None, "completed"):
                raise RuntimeError(
                    "Incomplete model response: "
                    + str(result.get("error") or result.get("incomplete_details"))
                )
            output = result.get("output", [])
            self.history.extend(output)
            with (self.root / "model" / "transcript.jsonl").open("a") as f:
                f.write(json.dumps({"at": time.time(), "response": result}) + "\n")
            pending = [i for i in output if i.get("type") == "function_call"]
            if not pending:
                self.cycles += 1
                return {"ok": True, "tool_calls": calls}
            for call in pending:
                calls += 1
                if calls > 40:
                    raise RuntimeError(
                        "Reasoning exceeded 40 tools; yielding to queued messages."
                    )
                if call["name"] not in {t["name"] for t in tools}:
                    receipt = {"ok": False, "error": "Unknown tool"}
                else:
                    try:
                        receipt = execute(call["name"], json.loads(call["arguments"]))
                    except (ValueError, TypeError) as e:
                        receipt = {"ok": False, "error": str(e)}
                self.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(receipt),
                    }
                )
        raise RuntimeError("Model call stopped.")
