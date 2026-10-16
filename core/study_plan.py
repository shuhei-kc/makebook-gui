"""A durable research plan within one immutable campaign and one writer.

The campaign start is the tree anchor. A focus supplies legal context moves;
those moves are never selections, learned moves, or played-game evidence.
Each game freezes its focus and repertoire at dispatch. Updating the plan only
affects games that have not started, including after a manager restart.
"""
from copy import deepcopy
import cshogi
from .learning import key_of


def default_plan(config):
    if config.get('sequential_study'):
        focuses = deepcopy(config['sequential_study']['focuses'])
        return {'revision': 'initial', 'focus': focuses[0],
                'fixed_rules': deepcopy(config.get('fixed_rules', [])),
                'focuses': {f['id']: f for f in focuses}}
    return {'revision': 'initial', 'focus': {'id': 'anchor', 'label': '共通起点', 'moves': []},
            'fixed_rules': deepcopy(config.get('fixed_rules', [])),
            'focuses':{'anchor':{'id':'anchor','label':'共通起点','moves':[]}}}


def focus_position(config, focus):
    from .runner import position_board
    board, origin, prefix, *_ = position_board(config['start_position'])
    if not isinstance(focus,dict):
        raise ValueError('Focus must be an object')
    moves = focus.get('moves')
    if not isinstance(moves, list) or not all(isinstance(m, str) for m in moves):
        raise ValueError('Focus requires a list of context moves from the tree anchor')
    for move in moves:
        parsed = board.move_from_usi(move)
        if not board.is_legal(parsed):
            raise ValueError('Illegal focus move: ' + move)
        board.push(parsed)
    if board.move_number - 1 >= config['max_total_plies']:
        raise ValueError('Focus reaches the total-ply limit')
    position = 'position ' + origin
    full = prefix + moves
    if full:
        position += ' moves ' + ' '.join(full)
    _, _, _, keys, checks = position_board(position)
    from .runner import repetition
    if repetition(keys, checks) or board.is_game_over():
        raise ValueError('Focus is already terminal')
    return position


def update_plan(config, old, request):
    from .tasks import valid_id
    from .runner import position_board
    if request.get('expected_plan_revision') != old['revision']:
        raise ValueError('Study plan changed; reload its revision before updating')
    plan = deepcopy(old)
    if 'focus' in request:
        focus = deepcopy(request['focus'])
        if not isinstance(focus,dict):
            raise ValueError('Focus must be an object')
        if config.get('sequential_study') and focus != old['focus']:
            raise ValueError('Sequential tasks advance only after the required black winning streak')
        valid_id(focus.get('id'))
        if not isinstance(focus.get('label'), str) or not focus['label'].strip() or len(focus['label'])>500:
            raise ValueError('Focus requires a label')
        focus_position(config, focus)
        registry=plan.setdefault('focuses',{plan['focus']['id']:deepcopy(plan['focus'])})
        if focus['id'] in registry and registry[focus['id']]['moves']!=focus['moves']:
            raise ValueError('A focus ID cannot be reused for another position')
        registry[focus['id']]=deepcopy(focus)
        plan['focus'] = focus
    rules = {r['canonical_sfen']: r for r in plan['fixed_rules']}
    additions = request.get('fixed_rules', [])
    removals = request.get('remove_fixed_keys', [])
    if not isinstance(additions, list) or len(additions) > 256 or not isinstance(removals, list):
        raise ValueError('Invalid repertoire update')
    if any(not isinstance(r,dict) for r in additions):
        raise ValueError('Fixed rules must be objects')
    if len({r['canonical_sfen'] for r in additions}) != len(additions):
        raise ValueError('Duplicate fixed-rule position')
    for key in removals:
        if key not in rules:
            raise ValueError('Cannot remove an unknown fixed rule')
        del rules[key]
    for rule in additions:
        # Require an auditable legal route to each protected rule.
        path = rule.get('path')
        board, *_ = position_board(focus_position(config, {'moves': path}))
        if key_of(board) != rule['canonical_sfen']:
            raise ValueError('Fixed-rule path does not reach its position')
        choices = rule.get('moves')
        if not isinstance(choices, list) or not choices:
            raise ValueError('Fixed rule requires moves')
        if any(not isinstance(m,dict) for m in choices):
            raise ValueError('Fixed moves must be objects')
        if len({m['usi'] for m in choices}) != len(choices):
            raise ValueError('Duplicate fixed move')
        for item in choices:
            weight = item.get('weight', 1)
            if type(weight) not in (int, float) or not 0 < weight <= 1000000:
                raise ValueError('Invalid fixed-move weight')
            if not board.is_legal(board.move_from_usi(item['usi'])):
                raise ValueError('Illegal fixed move')
        if not isinstance(rule.get('reason'), str) or not rule['reason'].strip():
            raise ValueError('Fixed rule requires a review reason')
        rules[rule['canonical_sfen']] = deepcopy(rule)
    if not any(k in request for k in ('focus', 'fixed_rules', 'remove_fixed_keys')):
        raise ValueError('Empty study-plan update')
    plan['fixed_rules'] = list(rules.values())
    plan['revision'] = request['task_id']
    return plan


def game_start(config, plan):
    from .runner import position_board
    return position_board(focus_position(config, plan['focus']))
