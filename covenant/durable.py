"""Per-kingdom SQLite inbox, outbox and authoritative receipts."""

from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


def agent_directory(base, connection):
    origin = hashlib.sha256(connection.server.rstrip("/").encode()).hexdigest()[:16]
    path = (
        Path(base).resolve()
        / "servers"
        / origin
        / "games"
        / connection.gameId
        / "agents"
        / connection.playerId
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Journal:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            Path(root) / "journal.sqlite", check_same_thread=False
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,cursor INTEGER NOT NULL,event TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'received',reason TEXT,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY,command TEXT NOT NULL,receipt TEXT,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS diagnostics(at REAL NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
        """)
        # A process crash re-presents unacknowledged events, not already handled ones.
        self.db.execute("UPDATE inbox SET state='received' WHERE state='presented'")
        self.db.commit()
        if self.get("reply_receipt_migration", 0) < 1:
            for (raw,) in self.db.execute(
                "SELECT data FROM diagnostics WHERE kind='tool_receipt'"
            ).fetchall():
                item = json.loads(raw)
                args = item.get("arguments", {})
                result = item.get("result") or {}
                if (
                    result.get("ok")
                    and item.get("tool") == "send_message"
                    and args.get("reply_to")
                ):
                    self.mark(
                        ["message:" + args["reply_to"]],
                        "answered",
                        (result.get("result") or {}).get("id"),
                    )
                elif (
                    result.get("ok")
                    and item.get("tool") == "no_reply"
                    and args.get("message_id")
                ):
                    self.mark(
                        ["message:" + args["message_id"]],
                        "no_reply",
                        args.get("reason"),
                    )
            self.set("reply_receipt_migration", 1)

    def get(self, key, default=None):
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def receive(self, events, cursor):
        # The inbox and its reconnect cursor commit in the same durable transaction.
        with self.lock, self.db:
            for e in events:
                data = e.get("data", {})
                event_id = (
                    "message:" + data["id"]
                    if e.get("kind") == "message" and data.get("id")
                    else str(e.get("cursor", cursor)) + ":" + e["kind"]
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,cursor,event,at) VALUES(?,?,?,?)",
                    (event_id, e.get("cursor", cursor), json.dumps(e), time.time()),
                )
            self.db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("cursor", json.dumps(cursor)),
            )

    def pending(self, limit=100):
        with self.lock:
            return [
                {"inboxId": r[0], **json.loads(r[1])}
                for r in self.db.execute(
                    "SELECT id,event FROM inbox WHERE state='received' ORDER BY CASE WHEN json_extract(event,'$.kind')='message' THEN 0 ELSE 1 END,cursor LIMIT ?",
                    (limit,),
                )
            ]

    def mark(self, ids, state, reason=None):
        with self.lock, self.db:
            for event_id in ids:
                self.db.execute(
                    "UPDATE inbox SET state=?,reason=? WHERE id=?",
                    (state, reason, event_id),
                )

    def messages(self):
        with self.lock:
            return [
                {"id": r[0], "event": json.loads(r[1]), "state": r[2], "reason": r[3]}
                for r in self.db.execute(
                    "SELECT id,event,state,reason FROM inbox WHERE json_extract(event,'$.kind')='message' ORDER BY cursor"
                )
            ]

    def retry_presented(self, ids):
        # A failed reasoning cycle must never erase already completed replies or no-reply decisions.
        with self.lock, self.db:
            for event_id in ids:
                self.db.execute(
                    "UPDATE inbox SET state='received' WHERE id=? AND state='presented'",
                    (event_id,),
                )

    def enqueue(self, key, command):
        raw = json.dumps(command, sort_keys=True)
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT command FROM outbox WHERE key=?", (key,)
            ).fetchone()
            if old and old[0] != raw:
                raise ValueError("An action ID was reused for different content.")
            self.db.execute(
                "INSERT OR IGNORE INTO outbox(key,command,at) VALUES(?,?,?)",
                (key, raw, time.time()),
            )

    def outgoing(self):
        with self.lock:
            return [
                (k, json.loads(c))
                for k, c in self.db.execute(
                    "SELECT key,command FROM outbox WHERE receipt IS NULL ORDER BY at LIMIT 24"
                )
            ]

    def complete(self, key, receipt):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE outbox SET receipt=? WHERE key=?", (json.dumps(receipt), key)
            )
            row = self.db.execute(
                "SELECT command FROM outbox WHERE key=?", (key,)
            ).fetchone()
            command = json.loads(row[0]) if row else {}
            if receipt.get("ok") and command.get("replyTo"):
                self.db.execute(
                    "UPDATE inbox SET state='answered',reason=? WHERE id=?",
                    (
                        (receipt.get("result") or {}).get("id"),
                        "message:" + command["replyTo"],
                    ),
                )

    def receipt(self, key):
        with self.lock:
            r = self.db.execute(
                "SELECT receipt FROM outbox WHERE key=?", (key,)
            ).fetchone()
            return json.loads(r[0]) if r and r[0] else None

    def receipts(self, limit=30):
        with self.lock:
            return [
                {"key": k, "command": json.loads(c), "receipt": json.loads(r)}
                for k, c, r in self.db.execute(
                    "SELECT key,command,receipt FROM outbox WHERE receipt IS NOT NULL ORDER BY at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def log(self, kind, data):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO diagnostics VALUES(?,?,?)",
                (time.time(), kind, json.dumps(data)),
            )

    def close(self):
        with self.lock:
            self.db.close()
