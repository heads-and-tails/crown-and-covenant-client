"""Outbound HTTPS transport. Model credentials never go to the game server."""

from __future__ import annotations
import json
import os
import re
import time
import uuid
import copy
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse


class ProtocolError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "NETWORK_ERROR"):
        super().__init__(message)
        self.status, self.code = status, code


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(
            os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(repr=False)
class Connection:
    server: str
    gameId: str
    playerId: str
    token: str

    def __post_init__(self):
        self.server = self.server.rstrip("/")
        parsed = urlparse(self.server)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Server must be an HTTP(S) origin without credentials or query parameters."
            )
        if not re.fullmatch(r"[A-F0-9]{8}", self.gameId) or not re.fullmatch(
            r"p[1-4]", self.playerId
        ):
            raise ValueError("Invalid game or player identifier.")
        if not isinstance(self.token, str) or len(self.token) < 20:
            raise ValueError("Invalid player token.")

    def __repr__(self):
        return f"Connection(server={self.server!r}, gameId={self.gameId!r}, playerId={self.playerId!r}, token=<redacted>)"

    @classmethod
    def load(cls, path: str | Path) -> "Connection":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: data[k] for k in ("server", "gameId", "playerId", "token")})

    def save(self, path: str | Path) -> None:
        atomic_json(Path(path), asdict(self))


class Client:
    def __init__(self, connection: Connection, timeout: float = 12, retries: int = 3):
        self.connection, self.timeout, self.retries = connection, timeout, retries
        self.clock_offset = 0.0
        self._map = None
        self._observation = None
        self.revision = -1
        self._cache_cursor = 0
        self._lock = threading.RLock()

    def _request(
        self, path: str, body: dict | None = None, key: str | None = None
    ) -> dict:
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.connection.token,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode()
        if key:
            headers["Idempotency-Key"] = key
        for attempt in range(self.retries):
            try:
                request = Request(
                    self.connection.server + "/api" + path, data=data, headers=headers
                )
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response)
                obs = result.get("observation", result)
                if "serverTime" in obs:
                    self.clock_offset = obs["serverTime"] / 1000 - time.time()
                return result
            except HTTPError as exc:
                try:
                    error = json.loads(exc.read(65536))
                except (ValueError, OSError):
                    error = {"error": f"HTTP {exc.code}", "code": "HTTP_ERROR"}
                if exc.code < 500 and exc.code != 429:
                    raise ProtocolError(
                        error.get("error", "Request failed"),
                        exc.code,
                        error.get("code", "HTTP_ERROR"),
                    ) from None
                if attempt == self.retries - 1:
                    raise ProtocolError(
                        error.get("error", "Server temporarily unavailable"), exc.code
                    ) from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries - 1:
                    raise ProtocolError(
                        "Cannot reach the game server; check the connection and retry."
                    ) from exc
            if attempt < self.retries - 1:
                time.sleep(min(4, 0.5 * 2**attempt))
        raise ProtocolError("Request failed.")

    def _fetch_state(self) -> dict:
        if self._map is None:
            self._map = self._request(f"/games/{self.connection.gameId}/map")
        observation = self._request(f"/games/{self.connection.gameId}/state?map=0")
        if observation.get("protocolVersion") != 3:
            raise ProtocolError(
                "This client needs a protocol v3 match. Historical matches are read-only archives.",
                code="VERSION_MISMATCH",
            )
        observation.update({"tiles": self._map["tiles"], "size": self._map["size"]})
        self._observation = observation
        self.revision = observation["revision"]
        self._cache_cursor = observation.get("eventCursor", 0)
        return copy.deepcopy(observation)

    def state(self, refresh: bool = False) -> dict:
        """Read the local cache. Runtime updates it; refresh=True explicitly resynchronizes."""
        with self._lock:
            if refresh or self._observation is None:
                self._fetch_state()
            return copy.deepcopy(self._observation)

    def _query(self, after):
        o = self._observation or {}
        query = f"after={after}&revision={self.revision}"
        if "turn-snapshots-v1" in o.get("capabilities", []):
            query += f"&sync=turn-v1&turn={o['turn']}&status={o['status']}"
        return query

    def _apply_update(self, update):
        if update.get("sync") != "turn-v1":
            if update.get("observation"):
                self._observation = {**update["observation"], "tiles": self._map["tiles"], "size": self._map["size"]}
                self.revision = self._observation["revision"]
        elif update.get("snapshotRequired"):
            self._fetch_state()
        else:
            o = copy.deepcopy(self._observation)
            for event in update.get("events", []):
                if event["kind"] != "state_patch" or event["cursor"] <= self._cache_cursor:
                    continue
                patch = event["data"]
                if (o["turn"], o["status"]) != (patch["turn"], patch["status"]):
                    continue
                o.update(patch["set"])
                for field, delta in patch["arrays"].items():
                    removed = set(delta["remove"])
                    items = {x["id"]: x for x in o.get(field, []) if x["id"] not in removed}
                    items.update({x["id"]: x for x in delta["upsert"]})
                    o[field] = list(items.values())
            self._cache_cursor = max(self._cache_cursor, update["cursor"])
            o["revision"] = max(o["revision"], update["revision"])
            o["eventCursor"] = self._cache_cursor
            self._observation = o
            self.revision = o["revision"]
        # The world may have advanced again during the snapshot request. Never regress it.
        if update.get("revision", -1) >= self.revision:
            self._observation["serverTime"] = update.get("serverTime", self._observation["serverTime"])
        update["observation"] = copy.deepcopy(self._observation)
        # Cache maintenance is internal, not another user callback.
        update["events"] = [e for e in update.get("events", []) if e["kind"] != "state_patch"]
        return update

    def updates(self, after: int = 0) -> dict:
        with self._lock:
            if self._observation is None:
                self._fetch_state()
            update = self._request(f"/games/{self.connection.gameId}/events?{self._query(after)}")
            return self._apply_update(update)

    def command(
        self, kind: str, data: dict | None = None, key: str | None = None
    ) -> dict:
        body = {"type": kind}
        if data is not None:
            body["data"] = data
        with self._lock:
            if self._observation is None:
                self._fetch_state()
            response = self._request(
                f"/games/{self.connection.gameId}/commands?{self._query(self._cache_cursor)}", body, key or uuid.uuid4().hex
            )
            return self._apply_update(response)

    def orders(self, orders: dict, key: str | None = None) -> dict:
        return self.command("orders", orders, key)

    def message(self, to: str, text: str, key: str | None = None) -> dict:
        return self.command("message", {"to": to, "text": text}, key)

    def offer(self, to: str, give: dict, want: dict, key: str | None = None) -> dict:
        return self.command(
            "offer", {"to": to, "kind": "trade", "give": give, "want": want}, key
        )

    def seconds_left(self, observation: dict) -> float:
        return (
            max(
                0,
                observation.get("deadline", 0) / 1000 - time.time() - self.clock_offset,
            )
            if observation.get("deadline")
            else 0
        )
