"""Immutable typed views over the protocol; mutable transport objects never escape."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class Record(Mapping):
    __slots__ = ("_data",)

    def __init__(self, data):
        object.__setattr__(
            self, "_data", MappingProxyType({k: freeze(v) for k, v in data.items()})
        )

    def __setattr__(self, key, value):
        raise TypeError("Game snapshots are read-only.")

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __getattr__(self, key):
        aliases = {
            "from_player": "from",
            "to_player": "to",
            "order_revision": "orderRevision",
        }
        try:
            return self._data[aliases.get(key, key)]
        except KeyError:
            raise AttributeError(key) from None

    def to_dict(self):
        return thaw(self)

    def __repr__(self):
        return f"{type(self).__name__}({dict(self._data)!r})"


def freeze(value):
    if isinstance(value, Record):
        return value
    if isinstance(value, dict):
        return Record(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value):
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


class Army(Record):
    id: str
    owner: str
    x: int
    y: int
    troops: Record
    route: tuple
    order_revision: int


class Structure(Record):
    id: str
    owner: str | None
    x: int
    y: int
    kind: str
    production: Record | None
    garrison: Record
    order_revision: int


class Player(Record):
    id: str
    name: str
    eliminated: bool


class GameState(Record):
    turn: int
    deadline: int | None
    treasury: Record
    income: Record
    rules: Record
    size: int

    def __init__(self, data):
        data = dict(data)
        for key, cls in [
            ("armies", Army),
            ("structures", Structure),
            ("players", Player),
        ]:
            data[key] = tuple(cls(v) for v in data.get(key, []))
        super().__init__(data)

    def _owner(self, owner):
        return self.you if owner == "me" else owner

    def get_player(self, player_id=None):
        return next((p for p in self.players if p.id == (player_id or self.you)), None)

    def get_armies(self, owner=None):
        return tuple(
            a for a in self.armies if owner is None or a.owner == self._owner(owner)
        )

    def get_army(self, id):
        return next((a for a in self.armies if a.id == id), None)

    def get_structures(self, owner=None, kind=None, resource=None):
        return tuple(
            s
            for s in self.structures
            if (owner is None or s.owner == self._owner(owner))
            and (kind is None or s.kind == kind)
            and (resource is None or s.get("resource") == resource)
        )

    def get_structure(self, id):
        return next((s for s in self.structures if s.id == id), None)

    def get_tile(self, x, y):
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x < self.size and 0 <= y < self.size)
        ):
            raise ValueError("Tile is outside the map.")
        return self.tiles[y * self.size + x]

    def get_orders(self):
        return self.currentOrders

    def get_trades(self, status=None):
        return tuple(
            o
            for o in self.offers
            if o.kind == "trade" and (status is None or o.status == status)
        )


@dataclass(frozen=True)
class RouteOrder:
    army: Army
    steps: tuple | list


@dataclass(frozen=True)
class ProductionOrder:
    castle: Structure
    troop: str | None
    quantity: int = 1
