"""Deterministic tactical control; high-level intent never needs to route tiles."""
from __future__ import annotations
from collections import deque
from typing import Callable

RESOURCES = ('grain', 'wood', 'iron', 'horses', 'crystal', 'stone')
TROOPS = ('militia', 'archer', 'pikeman', 'knight', 'mage', 'siege')


def pos(value: dict) -> tuple[int, int]:
    return value['x'], value['y']


def legal_step(obs: dict, start: tuple[int, int], end: tuple[int, int]) -> bool:
    x, y = end
    sx, sy = start
    size = obs['size']
    if type(x) is not int or type(y) is not int or not (0 <= x < size and 0 <= y < size) or max(abs(x-sx), abs(y-sy)) != 1:
        return False
    def ok(px, py):
        return obs['tiles'][py * size + px]['terrain'] not in ('mountain', 'river')
    return ok(x, y) and (x == sx or y == sy or (ok(x, sy) and ok(sx, y)))


def routes(obs: dict, start: tuple[int, int], blocked: set[tuple[int, int]] | None = None) -> Callable:
    blocked = blocked or set()
    queue = deque([start])
    parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    while queue:
        x, y = queue.popleft()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nxt = x+dx, y+dy
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


class TacticalController:
    def path(self, observation: dict, start: dict, target: dict) -> list[dict]:
        return [{'x': x, 'y': y} for x, y in routes(observation, pos(start))(pos(target))]

    def plan(self, observation: dict, intent: dict | None = None) -> dict:
        o, intent = observation, intent or {}
        you = o['you']
        me = next(p for p in o['players'] if p['id'] == you)
        units = o['rules']['units']
        strength = lambda a: sum(a['troops'].get(t, 0) * units[t]['power'] for t in TROOPS)
        allies = {you}
        own = sorted((a for a in o['armies'] if a['owner'] == you), key=lambda a: (-strength(a), a['id']))
        hostile = [a for a in o['armies'] if a['owner'] not in allies]
        blocked = {pos(a) for a in hostile}
        assigned, moves = set(), []
        stance = intent.get('stance', 'defend' if me.get('style') == 'defensive' else 'expand')
        for army in own:
            route = routes(o, pos(army), blocked)
            candidates = []
            for site in o['structures']:
                if site['owner'] in allies or site['owner'] in intent.get('avoidPlayers', []):
                    continue
                path = route(pos(site))
                defense = sum(strength(a) for a in hostile if pos(a) == pos(site)) + strength({'troops': site['garrison']})
                defense *= 1.6 if site['kind'] == 'castle' else 1
                value = (24 if site['kind'] == 'castle' else 7) + (6 if site['owner'] is None else 0)
                if site.get('resource') and o['treasury'][site['resource']] < 8:
                    value += 6
                if site['id'] in intent.get('castleTargets', []):
                    value += 40 - min(30, intent['castleTargets'].index(site['id']) * 5)
                if site['owner'] == intent.get('targetPlayer', ''):
                    value += 15
                if stance == 'attack' and site['owner']:
                    value += 20
                if path and site['id'] not in assigned and strength(army) > defense * (1.0 if me.get('style') == 'aggressive' else 1.2):
                    candidates.append((value / (len(path)+1), site))
            destination = intent.get('targets', {}).get(army['id']) or next((v for v in intent.get('armyObjectives', []) if v.get('armyId') == army['id']), None)
            if not destination and army != own[0] and strength(army) < 35:
                destination = own[0]
            if not destination and (stance == 'defend' or intent.get('defensivePriorities')):
                destination = next((s for s in o['structures'] if s['owner'] == you and s['kind'] == 'castle' and (stance == 'defend' or s['id'] in intent.get('defensivePriorities', [])) and any(max(abs(a['x']-s['x']), abs(a['y']-s['y'])) <= 3 for a in hostile)), None)
            if not destination and candidates:
                candidates.sort(key=lambda c: -c[0])
                destination = candidates[0][1]
                assigned.add(destination['id'])
            if not destination and army != own[0]:
                destination = own[0]
            if destination:
                path = route(pos(destination))
                if path:
                    enemy = next((a for a in hostile if pos(a) == path[0]), None)
                    if enemy is None or strength(army) > strength(enemy) * 1.25:
                        moves.append({'armyId': army['id'], 'x': path[0][0], 'y': path[0][1]})
        castles = sorted((s for s in o['structures'] if s['kind'] == 'castle' and s['owner'] == you), key=lambda s: s['id'])
        budget = dict(o['treasury'])
        budget['grain'] += len(castles) * o['rules']['castleIncome']
        for resource, reserve in intent.get('reserves', {}).items():
            if resource in budget and type(reserve) is int:
                budget[resource] = max(0, budget[resource] - max(0, reserve))
        preference = ['mage', 'knight', 'archer', 'pikeman', 'militia']
        if me.get('style') == 'aggressive':
            preference = ['knight', 'mage', 'archer', 'pikeman', 'militia']
        if me.get('style') == 'defensive':
            preference = ['mage', 'pikeman', 'archer', 'knight', 'militia']
        if intent.get('preferredTroop') in TROOPS:
            preference.insert(0, intent['preferredTroop'])
        elif sum(a['troops']['knight'] for a in hostile) > sum(a['troops']['archer'] for a in hostile):
            preference.insert(0, 'pikeman')
        if o['turn'] > 12 and sum(a['troops']['siege'] for a in own) < 3:
            preference.insert(0, 'siege')
        composition = intent.get('composition', {})
        if composition:
            counts = {t: sum(a['troops'].get(t, 0) for a in own) for t in TROOPS}
            total = max(1, sum(counts.values()))
            desired = sorted((t for t in TROOPS if composition.get(t, 0) > 0), key=lambda t: -(composition[t] / 100 - counts[t] / total))
            preference = desired + preference
        production = []
        for castle in castles:
            selected, count = 'militia', 0
            for troop in preference:
                cost = units[troop]['cost']
                count = min(o['rules']['productionCapacity'], *(budget[r] // qty for r, qty in cost.items()))
                if count >= 2 or troop == 'militia':
                    selected, count = troop, max(0, count)
                    break
            for resource, qty in units[selected]['cost'].items():
                budget[resource] -= qty * count
            production.append({'castleId': castle['id'], 'troop': selected if count else None, 'count': max(1, count)})
        return {'turn': o['turn'], 'moves': moves, 'production': production, 'ready': True}
