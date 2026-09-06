"""Opt-in real subscription test against the isolated local fixture; never a paid API call."""

import argparse, json, os, sys, time, threading, urllib.request, http.cookiejar, uuid
from pathlib import Path
from covenant.catalog import initialize
from covenant.supervisor import Supervisor
from covenant.transport import Client, Connection

p = argparse.ArgumentParser()
p.add_argument("--root", required=True)
p.add_argument("--resume", action="store_true")
p.add_argument("--kind", choices=["classic", "procedural"], default="classic")
p.add_argument("--horizon", type=int, default=350)
p.add_argument("--models", type=int, default=4)
p.add_argument("--seed", type=int, default=5001)
p.add_argument("--seconds", type=int, default=1500)
p.add_argument("--crash-test", action="store_true")
a = p.parse_args()
base = "http://127.0.0.1:3014"
root = Path(a.root)
initialize(root)
for folder in ("codex", "tactical"):
    if "poll_seconds" not in (root / "agents" / folder / "agent.toml").read_text():
        with (root / "agents" / folder / "agent.toml").open("a") as f:
            f.write("\npoll_seconds = 0.2\n")
jar = http.cookiejar.CookieJar()
browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def req(path, body=None):
    r = urllib.request.Request(
        base + "/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Origin": base},
    )
    with browser.open(r, timeout=30) as f:
        return json.load(f)


req("/auth")
req("/auth/google", {"credential": "admin-credential"})
g = (
    json.loads(
        json.loads((root / "browser-state.json").read_text())["origins"][0][
            "localStorage"
        ][0]["value"]
    )[0]
    if a.resume
    else req(
        "/games",
        {
            "playerName": "Test kingdom",
            "settings": {"kind": a.kind},
            "seed": a.seed,
            "turnSeconds": 180,
        },
    )
)
s = Supervisor(base, root, label="v4 verification")
s.register()
if s.credentials.get("pairCode"):
    req("/clients/pair", {"code": s.credentials["pairCode"]})
s.step()
catalog = req("/clients")["clients"]
h = next(h for h in catalog if h["id"] == s.credentials["id"])
for seat in range(0 if a.resume else 4):
    definition = next(
        d
        for d in h["catalog"]
        if d["id"] == ("codex" if seat < a.models else "tactical")
    )
    req(
        "/games/" + g["gameId"] + "/agents",
        {
            "seat": seat,
            "clientId": h["id"],
            "definitionId": definition["id"],
            "definitionVersion": definition["version"],
            "name": ["Alder", "Birch", "Cedar", "Dogwood"][seat],
            "requestId": uuid.uuid4().hex,
        },
    )
root.mkdir(exist_ok=True)
(root / "test-game.json").write_text(
    json.dumps({"gameId": g["gameId"], "kind": a.kind})
)
# A local-only browser test account; no production credential is saved here.
(root / "browser-state.json").write_text(
    json.dumps(
        {
            "cookies": [
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": False,
                    "sameSite": "Lax",
                }
                for c in jar
            ],
            "origins": [
                {
                    "origin": base,
                    "localStorage": [
                        {"name": "covenant.sessions", "value": json.dumps([g])}
                    ],
                }
            ],
        }
    )
)
stop = threading.Event()
errors = []


def supervise():
    while not stop.is_set():
        try:
            s.step()
        except Exception as e:
            errors.append(str(e))
            print("SUPERVISOR", str(e), flush=True)
        stop.wait(0.5)


t = threading.Thread(target=supervise, daemon=True)
t.start()
players = {}
started = time.time()
report = {}
try:
    while time.time() - started < 60:
        if len(s.workers) == 4 and all(
            (w.private / "status.json").exists()
            and json.loads((w.private / "status.json").read_text())
            .get("stats", {})
            .get("callbacks", 0)
            > 0
            for w in list(s.workers.values())
        ):
            break
        time.sleep(0.5)
    players = {
        w.job["playerId"]: Client(
            Connection.load(w.private / "connection.json"), timeout=10, retries=1
        )
        for w in list(s.workers.values())
    }
    assert len(players) == 4, "Four workers failed to start"
    owner = Client(Connection(base, g["gameId"], "p1", g["token"]))
    while time.time() - started < 90:
        state = owner.state()
        if all(
            p.get("lastSeen") and p.get("agentStatus") not in ("retrying", "offline")
            for p in state["players"]
        ):
            break
        time.sleep(0.5)
    if owner.state()["status"] == "lobby":
        owner.command("start", {}, uuid.uuid4().hex)
    players["p1"].message(
        "p2",
        "I want a non-aggression agreement through turn 8. Which resources do you need, and what can you offer?",
    )
    players["p3"].message(
        "p4",
        "Which nearby resource sites are your priorities? Can we avoid competing for the same sites and trade later?",
    )
    print("STARTED", g["gameId"], a.kind, flush=True)
    # Keep first turn open to verify sustained replies and successful real-model script execution.
    sent_followup = False
    while time.time() - started < 240:
        memories = []
        for w in list(s.workers.values()):
            path = w.root / "workspace" / "agent-state.json"
            if path.exists():
                try:
                    memories.append(json.loads(path.read_text()))
                except ValueError:
                    pass
        if not sent_followup and sum(m["metrics"]["successes"] for m in memories) >= 2:
            players["p1"].message(
                "p2",
                "Following up: I can reserve 8 grain for a trade. What exact exchange would you consider after you capture your next resource site?",
            )
            sent_followup = True
        if len(memories) == a.models and all(
            m["metrics"]["successes"] >= 2 and m["metrics"]["script_successes"] >= 1
            for m in memories
        ):
            break
        time.sleep(2)
    print("MODEL_WARMUP", [(m["metrics"]) for m in memories], flush=True)
    last = 0
    stall = 0
    crashed = False
    while time.time() - started < a.seconds:
        try:
            o = owner.state()
        except Exception as e:
            errors.append(str(e))
            time.sleep(2)
            continue
        if o["status"] == "finished" or o["turn"] > a.horizon:
            break
        if o["turn"] != last:
            last = o["turn"]
            stall = time.time()
            if last % 10 == 1:
                print("TURN", last, flush=True)
            if a.crash_test and last >= 8 and not crashed:
                victim = next(
                    w for w in s.workers.values() if w.job["playerId"] == "p2"
                )
                (victim.root / "workspace" / "recovery-sentinel.txt").write_text(
                    "preserve me"
                )
                os.kill(victim.process.pid, 9)
                crashed = True
                print("WORKER_CRASH_INJECTED p2", flush=True)
            if (
                last in (4, 10, 20)
                and not next(p for p in o["players"] if p["id"] == "p2")["eliminated"]
            ):
                players["p1"].message(
                    "p2",
                    f"Turn {last}: what changed in your campaign, and is our proposed trade still useful?",
                )
        # Every live tactical controller plans once before the authoritative resolution.
        ready = True
        for w in list(s.workers.values()):
            if next(p for p in o["players"] if p["id"] == w.job["playerId"])[
                "eliminated"
            ]:
                continue
            file = (
                w.root
                / "workspace"
                / (
                    "agent-state.json"
                    if w.job["definitionId"] == "codex"
                    else "tactics.json"
                )
            )
            try:
                memory = json.loads(file.read_text())
                memory = memory.get("tactics", memory)
                if (
                    memory.get("last_applied_turn", memory.get("last_planned_turn", 0))
                    < last
                ):
                    ready = False
            except (OSError, ValueError):
                ready = False
        if ready or time.time() - stall > 8:
            for p in o["players"]:
                if not p["eliminated"]:
                    try:
                        players[p["id"]].command(
                            "ready", {"turn": last}, uuid.uuid4().hex
                        )
                    except Exception as e:
                        if (
                            "turn" not in str(e).lower()
                            and "finished" not in str(e).lower()
                        ):
                            print("READY", str(e), flush=True)
        time.sleep(0.3)
    # Keep bounded unfinished games truthful; do not invent a turn-limit winner.
    o = owner.state()
    report = {
        "gameId": g["gameId"],
        "kind": a.kind,
        "turn": o["turn"],
        "status": o["status"],
        "winner": o.get("winners"),
        "castles": o.get("standings"),
        "errors": errors,
        "crashInjected": crashed,
        "agents": {},
    }
    for folder in (root / "instances").glob("*/*/*"):
        path = folder / "workspace" / "agent-state.json"
        job = json.loads((folder / "runtime" / "assignment.json").read_text())
        if path.exists():
            m = json.loads(path.read_text())
            report["agents"][job["playerId"]] = {
                "metrics": m["metrics"],
                "messages": {k: v["status"] for k, v in m["messages"].items()},
                "completed": len(m["tactics"].get("completed", [])),
                "scripts": list(m["scripts"]),
            }
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print("RESULT", json.dumps(report), flush=True)
finally:
    stop.set()
    t.join(10)
    for w in list(s.workers.values()):
        w.close()
