from .controller import TROOPS, legal_step, pos


def validate_orders(orders: dict, observation: dict) -> dict:
    """Reject malformed or illegal custom/file orders before sending them."""
    if not isinstance(orders, dict) or set(orders) - {'turn', 'moves', 'production', 'ready'}:
        raise ValueError('Orders must contain only turn, moves, production, and ready.')
    if type(orders.get('turn')) is not int or orders['turn'] != observation['turn']:
        raise ValueError('Orders must match the current turn.')
    if not isinstance(orders.get('moves'), list) or len(orders['moves']) > 100:
        raise ValueError('moves must be a list with at most 100 entries.')
    if not isinstance(orders.get('production'), list) or len(orders['production']) > 30:
        raise ValueError('production must be a list with at most 30 entries.')
    if type(orders.get('ready', False)) is not bool:
        raise ValueError('ready must be a boolean.')
    armies = {a['id']: a for a in observation['armies'] if a['owner'] == observation['you']}
    castles = {s['id'] for s in observation['structures'] if s['owner'] == observation['you'] and s['kind'] == 'castle'}
    seen = set()
    for move in orders['moves']:
        if not isinstance(move, dict) or set(move) != {'armyId', 'x', 'y'}:
            raise ValueError('Every move needs armyId, x, and y.')
        if move['armyId'] not in armies or move['armyId'] in seen:
            raise ValueError('Duplicate or foreign army order.')
        seen.add(move['armyId'])
        if not legal_step(observation, pos(armies[move['armyId']]), (move['x'], move['y'])):
            raise ValueError('Illegal army movement.')
    seen = set()
    for item in orders['production']:
        if not isinstance(item, dict) or set(item) != {'castleId', 'troop', 'count'}:
            raise ValueError('Production needs castleId, troop, and count.')
        if item['castleId'] not in castles or item['castleId'] in seen:
            raise ValueError('Duplicate or foreign castle order.')
        seen.add(item['castleId'])
        if item['troop'] is not None and item['troop'] not in TROOPS:
            raise ValueError('Unknown troop type.')
        if type(item['count']) is not int or not 1 <= item['count'] <= observation['rules']['productionCapacity']:
            raise ValueError('Invalid recruitment count.')
    return {**orders, 'ready': orders.get('ready', False)}
