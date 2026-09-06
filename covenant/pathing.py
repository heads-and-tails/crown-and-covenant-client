"""Provider-independent legal paths over the cached static map."""

from collections import deque
from typing import Callable


def pos(value: dict) -> tuple[int, int]:
    return value["x"], value["y"]


def legal_step(obs: dict, start: tuple[int, int], end: tuple[int, int]) -> bool:
    x, y = end
    sx, sy = start
    size = obs["size"]
    if (
        type(x) is not int
        or type(y) is not int
        or not (0 <= x < size and 0 <= y < size)
        or max(abs(x - sx), abs(y - sy)) != 1
    ):
        return False

    def ok(px, py):
        return obs["tiles"][py * size + px]["terrain"] not in ("mountain", "river")

    return ok(x, y) and (x == sx or y == sy or (ok(x, sy) and ok(sx, y)))


def routes(
    obs: dict, start: tuple[int, int], blocked: set[tuple[int, int]] | None = None
) -> Callable:
    blocked = blocked or set()
    queue = deque([start])
    parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    while queue:
        x, y = queue.popleft()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nxt = x + dx, y + dy
                if nxt not in parents and legal_step(obs, (x, y), nxt):
                    parents[nxt] = (x, y)
                    if nxt not in blocked:
                        queue.append(nxt)

    def path(end):
        if end not in parents:
            return []
        result = []
        while end != start:
            result.append(end)
            end = parents[end]
        return result[::-1]

    return path
