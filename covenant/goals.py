"""Validate model-authored goals before they can enter persistent tactical state."""

from .controller import TROOPS, RESOURCES


def validate_goal(goal):
    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def identifier(value, label):
        require(
            isinstance(value, str) and 0 < len(value) <= 80,
            label + " must be an entity ID.",
        )

    def position(value):
        require(
            isinstance(value, dict)
            and set(value) == {"x", "y"}
            and all(type(v) is int and 0 <= v < 65 for v in value.values()),
            "Destination must be {x: integer, y: integer}.",
        )

    require(isinstance(goal, dict), "Goal must be an object or null.")
    if "untilTurn" in goal:
        require(
            type(goal["untilTurn"]) is int and goal["untilTurn"] >= 1,
            "untilTurn must be a positive turn number.",
        )
    kind = goal.get("kind")
    require(
        kind in ("strategy", "capture", "defend", "rally", "recruit", "hold"),
        "Unknown tactical goal kind.",
    )
    if kind in ("capture", "defend"):
        identifier(goal.get("structureId"), "structureId")
    if kind in ("rally", "hold") or "armyId" in goal:
        identifier(goal.get("armyId"), "armyId")
    if kind == "rally":
        position(goal.get("destination"))
    if kind == "recruit":
        identifier(goal.get("castleId"), "castleId")
        require(
            goal.get("troop") in TROOPS or goal.get("troop") is None,
            "Unknown troop type.",
        )
        require(
            type(goal.get("count", 1)) is int and 1 <= goal.get("count", 1) <= 6,
            "Recruitment count must be 1–6.",
        )
    if kind == "strategy":
        intent = goal.get("intent", {})
        require(isinstance(intent, dict), "intent must be an object.")
        supported = {
            "stance",
            "castleTargets",
            "defensivePriorities",
            "composition",
            "reserves",
            "avoidPlayers",
            "preferredTroop",
            "targetPlayer",
            "targets",
            "armyObjectives",
        }
        require(
            not set(intent) - supported,
            "Unsupported intent field: " + str(set(intent) - supported),
        )
        if "stance" in intent:
            require(
                intent["stance"] in ("expand", "attack", "defend"),
                "stance must be expand, attack or defend.",
            )
        for key in ("castleTargets", "defensivePriorities", "avoidPlayers"):
            if key in intent:
                require(
                    isinstance(intent[key], list) and len(intent[key]) <= 100,
                    key + " must be an array of IDs.",
                )
                for value in intent[key]:
                    identifier(value, key)
        for key, allowed in (("reserves", RESOURCES), ("composition", TROOPS)):
            if key in intent:
                require(
                    isinstance(intent[key], dict),
                    key
                    + " must be an object mapping names to quantities, for example "
                    + (
                        '{"grain":6}.'
                        if key == "reserves"
                        else '{"militia":70,"archer":30}.'
                    ),
                )
                require(
                    all(
                        k in allowed and type(v) is int and 0 <= v <= 10000
                        for k, v in intent[key].items()
                    ),
                    key + " contains invalid names or nonnegative integer quantities.",
                )
        if "preferredTroop" in intent:
            require(
                intent["preferredTroop"] in TROOPS,
                "preferredTroop must name a troop type.",
            )
        if "targetPlayer" in intent:
            identifier(intent["targetPlayer"], "targetPlayer")
        if "targets" in intent:
            require(
                isinstance(intent["targets"], dict),
                "targets must map army IDs to destinations.",
            )
            for key, value in intent["targets"].items():
                identifier(key, "armyId")
                position(value)
        if "armyObjectives" in intent:
            require(
                isinstance(intent["armyObjectives"], list),
                "armyObjectives must be an array.",
            )
            for value in intent["armyObjectives"]:
                require(isinstance(value, dict), "Army objective must be an object.")
                identifier(value.get("armyId"), "armyId")
                position({k: value.get(k) for k in ("x", "y")})
    return goal
