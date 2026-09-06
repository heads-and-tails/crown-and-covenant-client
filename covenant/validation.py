from .controller import TROOPS, legal_step, pos


def validate_orders(orders: dict, observation: dict) -> dict:
    """Reject malformed or illegal custom/file orders before sending them."""
    if isinstance(orders, dict) and "turn" not in orders:
        return validate_patch(orders, observation)
    if not isinstance(orders, dict) or set(orders) - {
        "turn",
        "moves",
        "production",
        "ready",
    }:
        raise ValueError("Orders must contain only turn, moves, production, and ready.")
    if type(orders.get("turn")) is not int or orders["turn"] != observation["turn"]:
        raise ValueError("Orders must match the current turn.")
    if not isinstance(orders.get("moves"), list) or len(orders["moves"]) > 100:
        raise ValueError("moves must be a list with at most 100 entries.")
    if (
        not isinstance(orders.get("production"), list)
        or len(orders["production"]) > 100
    ):
        raise ValueError("production must be a list with at most 100 entries.")
    if type(orders.get("ready", False)) is not bool:
        raise ValueError("ready must be a boolean.")
    armies = {
        a["id"]: a for a in observation["armies"] if a["owner"] == observation["you"]
    }
    castles = {
        s["id"]
        for s in observation["structures"]
        if s["owner"] == observation["you"] and s["kind"] == "castle"
    }
    seen = set()
    for move in orders["moves"]:
        if (
            not isinstance(move, dict)
            or not {"armyId", "x", "y"} <= set(move)
            or set(move) - {"armyId", "x", "y", "route"}
        ):
            raise ValueError("Every move needs armyId, x, and y.")
        if move["armyId"] not in armies or move["armyId"] in seen:
            raise ValueError("Duplicate or foreign army order.")
        seen.add(move["armyId"])
        if not legal_step(
            observation, pos(armies[move["armyId"]]), (move["x"], move["y"])
        ):
            raise ValueError("Illegal army movement.")
    seen = set()
    for item in orders["production"]:
        if not isinstance(item, dict) or set(item) != {"castleId", "troop", "count"}:
            raise ValueError("Production needs castleId, troop, and count.")
        if item["castleId"] not in castles or item["castleId"] in seen:
            raise ValueError("Duplicate or foreign castle order.")
        seen.add(item["castleId"])
        if item["troop"] is not None and item["troop"] not in TROOPS:
            raise ValueError("Unknown troop type.")
        if (
            type(item["count"]) is not int
            or not 1 <= item["count"] <= observation["rules"]["productionCapacity"]
        ):
            raise ValueError("Invalid recruitment count.")
    return {**orders, "ready": orders.get("ready", False)}


def validate_patch(patch, observation):
    if set(patch) - {"armies", "castles"}:
        raise ValueError("A v3 order patch contains armies and/or castles only.")
    armies = {
        a["id"]: a for a in observation["armies"] if a["owner"] == observation["you"]
    }
    castles = {
        s["id"]: s
        for s in observation["structures"]
        if s["owner"] == observation["you"] and s["kind"] == "castle"
    }
    seen = set()
    for entry in patch.get("armies", []):
        if not isinstance(entry, dict) or set(entry) != {
            "armyId",
            "from",
            "route",
            "revision",
        }:
            raise ValueError("Army orders need armyId, from, route and revision.")
        army = armies.get(entry["armyId"])
        if not army or army["id"] in seen:
            raise ValueError("Duplicate or foreign army order.")
        seen.add(army["id"])
        if (
            type(entry["revision"]) is not int
            or entry["revision"] != army.get("orderRevision", 0)
            or entry["from"] != {"x": army["x"], "y": army["y"]}
        ):
            raise ValueError("Stale army anchor or revision.")
        if (
            not isinstance(entry["route"], list)
            or len(entry["route"]) > observation["size"] ** 2
        ):
            raise ValueError("Invalid route length.")
        previous = pos(army)
        for step in entry["route"]:
            if (
                not isinstance(step, dict)
                or set(step) != {"x", "y"}
                or not legal_step(observation, previous, pos(step))
            ):
                raise ValueError("Illegal route step.")
            previous = pos(step)
    for entry in patch.get("castles", []):
        if not isinstance(entry, dict) or set(entry) != {
            "castleId",
            "production",
            "revision",
        }:
            raise ValueError("Castle orders need castleId, production and revision.")
        castle = castles.get(entry["castleId"])
        if not castle or castle["id"] in seen:
            raise ValueError("Duplicate or foreign castle order.")
        seen.add(castle["id"])
        if type(entry["revision"]) is not int or entry["revision"] != castle.get(
            "orderRevision", 0
        ):
            raise ValueError("Stale castle revision.")
        prod = entry["production"]
        if prod is not None and (
            not isinstance(prod, dict)
            or set(prod) != {"troop", "count"}
            or prod["troop"] not in TROOPS
            or type(prod["count"]) is not int
            or not 1 <= prod["count"] <= 6
        ):
            raise ValueError("Invalid production setting.")
    return patch
