"""Optional persistent objectives and combat-aware expansion. No provider dependencies."""

import math
from .pathing import routes, pos
from .constants import TROOPS, RESOURCES
from .state import RouteOrder, ProductionOrder


def combined(forces):
    return {t: sum(f.get(t, 0) for f in forces) for t in TROOPS}


def power(troops, enemy, rules, terrain="plains", castle=False):
    count = sum(enemy.values())
    total = 0
    for t in TROOPS:
        unit = rules["units"][t]
        counter = (
            sum(n * unit.get("counters", {}).get(e, 1) for e, n in enemy.items())
            / count
            if count
            else 1
        )
        bonus = (
            1.25
            if t == "archer" and terrain == "forest"
            else 1.2 if t == "knight" and terrain == "plains" else 1
        )
        total += troops.get(t, 0) * unit["power"] * counter * bonus
    defense = (rules.get("forestDefense", 0.15) if terrain == "forest" else 0) + (
        max(0, rules.get("castleDefense", 0.6) - enemy.get("siege", 0) * 0.15)
        if castle
        else 0
    )
    return total * (1 + defense)


def forecast(o, troops, site):
    enemies = [
        a["troops"]
        for a in o["armies"]
        if a["owner"] != o["you"] and pos(a) == pos(site)
    ]
    if site["owner"] != o["you"]:
        enemies.append(site["garrison"])
    enemy = combined(enemies)
    terrain = o["tiles"][site["y"] * o["size"] + site["x"]]["terrain"]
    attack = power(troops, enemy, o["rules"], terrain)
    defense = power(enemy, troops, o["rules"], terrain, site["kind"] == "castle")
    ratio = min(0.95, 0.65 * defense / max(attack, 1))
    survivors = {t: max(0, math.floor(troops.get(t, 0) * (1 - ratio))) for t in TROOPS}
    return {
        "attack": attack,
        "defense": defense,
        "wins": attack > defense + 1e-8,
        "survivors": survivors,
    }


class TacticalBot:
    def __init__(self, memory=None):
        self.memory = memory if memory is not None else {}
        self.memory.setdefault("objectives", {})
        self.memory.setdefault("directives", {})
        self.memory.setdefault("completed", [])

    def set_goal(self, id, goal):
        if goal is None:
            self.memory["directives"].pop(id, None)
        elif goal.get("kind") not in (
            "capture",
            "defend",
            "rally",
            "hold",
            "recruit",
            "strategy",
        ):
            raise ValueError("Use capture, defend, rally, hold, recruit, or strategy.")
        else:
            self.memory["directives"][id] = goal
        return {"ok": True, "goals": self.memory["directives"]}

    def plan(self, o):
        self.memory["last_planned_turn"] = o["turn"]
        self.memory["directives"] = {
            k: g
            for k, g in self.memory["directives"].items()
            if g.get("untilTurn", o["turn"]) >= o["turn"]
        }
        you = o["you"]
        rules = o["rules"]
        objectives = self.memory["objectives"]
        own = sorted(
            [a for a in o["armies"] if a["owner"] == you],
            key=lambda a: (
                -sum(a["troops"][t] * rules["units"][t]["power"] for t in TROOPS),
                a["id"],
            ),
        )
        owned = [s for s in o["structures"] if s["owner"] == you]
        castles = [s for s in owned if s["kind"] == "castle"]
        sites = {s["id"]: s for s in o["structures"]}
        armies = {a["id"]: a for a in own}
        for old, new in o.get("merges", {}).items():
            if old in objectives and new not in objectives:
                objectives[new] = objectives[old]
        for aid, goal in list(objectives.items()):
            if aid not in armies:
                objectives.pop(aid)
                continue
            site = sites.get(goal.get("target"))
            if goal.get("kind") == "capture" and site and site["owner"] == you:
                self.memory["completed"].append(
                    {
                        "turn": o["turn"],
                        "army": aid,
                        "target": site["id"],
                        "kind": site["kind"],
                        "resource": site.get("resource"),
                    }
                )
                objectives.pop(aid)
        self.memory["completed"] = self.memory["completed"][-200:]
        avoid = set()
        reserves = {}
        preferred = None
        for directive in self.memory["directives"].values():
            if directive["kind"] == "strategy":
                intent = directive.get("intent", {})
                avoid.update(intent.get("avoidPlayers", []))
                reserves.update(intent.get("reserves", {}))
                preferred = intent.get("preferredTroop", preferred)
        # Defend goals without an army choose one nearby force and keep it assigned.
        for directive in self.memory["directives"].values():
            if directive["kind"] == "defend" and directive.get("armyId") not in armies:
                site = sites.get(directive["structureId"])
                if site and own:
                    directive["armyId"] = min(
                        own,
                        key=lambda a: max(
                            abs(a["x"] - site["x"]), abs(a["y"] - site["y"])
                        ),
                    )["id"]
        hostile = [a for a in o["armies"] if a["owner"] != you]
        blocked = {pos(a) for a in hostile} | {
            pos(s) for s in o["structures"] if s["owner"] != you
        }
        claims = set()
        patch = {"armies": [], "castles": []}
        income = {r: 0 for r in RESOURCES}
        for s in owned:
            income["grain" if s["kind"] == "castle" else s["resource"]] += (
                rules["castleIncome"]
                if s["kind"] == "castle"
                else rules["resourceIncome"]
            )
        # Once neutral expansion is mostly over, gather a decisive force at one fixed castle.
        # The rendezvous remains stationary even while the expedition moves toward its target.
        campaign = self.memory.get("campaign")
        neutral_castles = [
            s for s in o["structures"] if s["kind"] == "castle" and s["owner"] is None
        ]
        if campaign and (
            sites.get(campaign["target"], {}).get("owner") in (you, None)
            or sites.get(campaign["rally"], {}).get("owner") != you
        ):
            campaign = None
        if campaign and not routes(o, pos(sites[campaign["rally"]]), blocked)(
            pos(sites[campaign["target"]])
        ):
            campaign = None
        if (
            not campaign
            and o["turn"] >= 35
            and len(castles) >= 2
            and len(neutral_castles) <= 1
        ):
            targets = [
                s
                for s in o["structures"]
                if s["kind"] == "castle" and s["owner"] not in {you, None, *avoid}
            ]
            pairs = [(c, s) for c in castles for s in targets]
            pairs.sort(
                key=lambda pair: max(
                    abs(pair[0]["x"] - pair[1]["x"]), abs(pair[0]["y"] - pair[1]["y"])
                )
                + forecast(o, {"militia": 1}, pair[1])["defense"] / 12
            )
            searches = {}
            for c, s in pairs[:12]:
                if c["id"] not in searches:
                    searches[c["id"]] = routes(o, pos(c), blocked)
                if searches[c["id"]](pos(s)):
                    campaign = {
                        "target": s["id"],
                        "rally": c["id"],
                        "started": o["turn"],
                    }
                    break
        self.memory["campaign"] = campaign

        def utility(s):
            if s["kind"] == "castle":
                return 38 + (
                    20 if len(castles) >= o.get("victoryTarget", 999) - 2 else 0
                )
            r = s["resource"]
            value = 10
            if income[r] == 0:
                value += 25 if r in ("wood", "iron") else 14
            if r == "grain":
                value += max(0, len(castles) * 6 - income["grain"]) * 3
            if r == "wood" and income[r] < income["grain"]:
                value += 12
            return value

        for army in own:
            path_to = routes(o, pos(army), blocked)
            directive = next(
                (
                    g
                    for g in self.memory["directives"].values()
                    if g.get("armyId") == army["id"]
                ),
                None,
            )
            target = None
            kind = "capture"
            reason = ""
            if directive and directive["kind"] == "hold":
                kind = "hold"
            elif directive and directive["kind"] == "rally":
                target = directive["destination"]
                kind = "rally"
            elif directive and directive["kind"] in ("capture", "defend"):
                target = sites.get(directive.get("structureId"))
                kind = directive["kind"]
            current = objectives.get(army["id"])
            if not directive and campaign:
                site = sites[campaign["target"]]
                rally = sites[campaign["rally"]]
                if forecast(o, army["troops"], site)["attack"] > forecast(
                    o, army["troops"], site
                )["defense"] * 1.25 and path_to(pos(site)):
                    target = site
                    kind = "capture"
                    reason = "Campaign force assembled; advancing on the castle."
                elif pos(army) == pos(rally) or path_to(pos(rally)):
                    target = rally
                    kind = "rally"
                    reason = "Gathering a decisive campaign force at a fixed castle."
            if (
                not target
                and kind != "hold"
                and current
                and current.get("kind") == "capture"
            ):
                site = sites.get(current.get("target"))
                if (
                    site
                    and site["owner"] != you
                    and site["owner"] not in avoid
                    and path_to(pos(site))
                ):
                    estimate = forecast(o, army["troops"], site)
                    if estimate["attack"] > estimate["defense"] * 1.1:
                        target = site
                    else:
                        reason = "Gathering strength: target defense increased."
            # A weak detachment reinforces at a fixed castle, never chases a moving army.
            if not target and kind != "hold":
                candidates = []
                requested = {
                    g.get("structureId")
                    for g in self.memory["directives"].values()
                    if g["kind"] == "capture"
                }
                for site in o["structures"]:
                    if (
                        site["owner"] == you
                        or site["owner"] in avoid
                        or site["id"] in claims
                    ):
                        continue
                    path = path_to(pos(site))
                    if not path:
                        continue
                    estimate = forecast(o, army["troops"], site)
                    if estimate["attack"] <= estimate["defense"] * 1.10:
                        continue
                    value = utility(site) + (60 if site["id"] in requested else 0)
                    candidates.append((value / (len(path) + 2), site))
                if candidates:
                    target = max(candidates, key=lambda item: (item[0], item[1]["id"]))[
                        1
                    ]
            if not target and kind != "hold" and castles:
                at = next((s for s in castles if pos(s) == pos(army)), None)
                if at:
                    target = at
                    kind = "rally"
                    reason = reason or "Gathering strength at a recruiting castle."
                else:
                    # Retain the rendezvous, even when another army becomes larger.
                    saved = (
                        sites.get(current.get("target"))
                        if current and current.get("kind") == "rally"
                        else None
                    )
                    options = [s for s in castles if path_to(pos(s))]
                    target = (
                        saved
                        if saved and saved["owner"] == you
                        else min(
                            options, key=lambda s: len(path_to(pos(s))), default=None
                        )
                    )
                    kind = "rally"
                    reason = reason or "Reinforcing at a fixed castle."
            if target and kind == "capture" and target.get("id"):
                claims.add(target["id"])
            path = path_to(pos(target)) if target else []
            # Check every intermediate structure/army, because direct model goals may be unsafe.
            if (
                target
                and target.get("owner") != you
                and "garrison" in target
                and kind == "capture"
                and not forecast(o, army["troops"], target)["wins"]
            ):
                path = []
                reason = (
                    "Blocked: insufficient force; recruit or assign reinforcements."
                )
            route = [{"x": x, "y": y} for x, y in path]
            if route != army.get("route", []):
                patch["armies"].append(
                    {
                        "armyId": army["id"],
                        "from": {"x": army["x"], "y": army["y"]},
                        "route": route,
                        "revision": army.get("orderRevision", 0),
                    }
                )
            objectives[army["id"]] = {
                "kind": kind,
                "target": target.get("id") if target else None,
                "destination": {"x": target["x"], "y": target["y"]} if target else None,
                "status": (
                    "travelling"
                    if path
                    else (
                        "holding"
                        if kind == "hold"
                        else (
                            "gathering_strength"
                            if kind == "rally"
                            else (
                                "holding"
                                if kind == "defend"
                                and target
                                and pos(army) == pos(target)
                                else "blocked"
                            )
                        )
                    )
                ),
                "reason": reason,
                "remainingTurns": len(path),
                "turn": o["turn"],
            }
        budget = {
            r: max(0, o["treasury"][r] + income[r] - reserves.get(r, 0))
            for r in RESOURCES
        }
        # Spend scarce grain efficiently. Archers are effective against early militia garrisons.
        enemy = combined([a["troops"] for a in hostile])
        default_enemy = enemy if sum(enemy.values()) else {"militia": 10}
        preference = sorted(
            TROOPS,
            key=lambda t: -(
                power({t: 1}, default_enemy, rules) / rules["units"][t]["cost"]["grain"]
            ),
        )
        if preferred in TROOPS:
            preference.insert(0, preferred)
        castles.sort(
            key=lambda s: (
                0 if campaign and s["id"] == campaign["rally"] else 1,
                min(
                    (
                        max(abs(s["x"] - e["x"]), abs(s["y"] - e["y"]))
                        for e in o["structures"]
                        if e["owner"] != you
                    ),
                    default=0,
                ),
                s["id"],
            )
        )
        for castle in castles:
            directive = next(
                (
                    g
                    for g in self.memory["directives"].values()
                    if g["kind"] == "recruit" and g.get("castleId") == castle["id"]
                ),
                None,
            )
            troop = None
            count = 1
            if directive:
                troop = directive.get("troop")
                count = directive.get("count", 1)
            else:
                for t in preference:
                    cost = rules["units"][t]["cost"]
                    n = min(
                        rules["productionCapacity"],
                        *(budget[r] // q for r, q in cost.items()),
                    )
                    if n >= 1:
                        troop = t
                        count = n
                        break
            production = {"troop": troop, "count": count} if troop else None
            if production != castle.get("production"):
                patch["castles"].append(
                    {
                        "castleId": castle["id"],
                        "production": production,
                        "revision": castle.get("orderRevision", 0),
                    }
                )
            if troop:
                for r, q in rules["units"][troop]["cost"].items():
                    budget[r] = max(0, budget[r] - q * count)
        return patch

    def step(self, ctx):
        state = ctx.get_state()
        patch = self.plan(state.to_dict())
        orders = []
        for a in patch["armies"]:
            orders.append(RouteOrder(state.get_army(a["armyId"]), a["route"]))
        for c in patch["castles"]:
            p = c["production"]
            orders.append(
                ProductionOrder(
                    state.get_structure(c["castleId"]),
                    p["troop"] if p else None,
                    p["count"] if p else 1,
                )
            )
        return ctx.submit_orders(orders) if orders else None
