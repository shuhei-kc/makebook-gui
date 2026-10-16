"""One focus at a time, with a durable streak of actual black wins.

Progress and the next focus are committed in the same transaction as the game.
The dispatcher reserves the remaining wins for in-flight games, so a focus can
reach its target only after every game started from that focus has finished.
"""
from copy import deepcopy
import time


def validate(config):
    settings = config.get('sequential_study')
    if settings is None:
        return
    if not isinstance(settings, dict):
        raise ValueError('sequential_study must be an object')
    target = settings.get('target_black_streak')
    if type(target) is not int or target < 1:
        raise ValueError('target_black_streak must be a positive integer')
    focuses = settings.get('focuses')
    if not isinstance(focuses, list) or not focuses:
        raise ValueError('Sequential study requires ordered focuses')
    from .tasks import valid_id
    from .study_plan import focus_position
    identities = set()
    for focus in focuses:
        if not isinstance(focus, dict):
            raise ValueError('Sequential focus must be an object')
        identity = valid_id(focus.get('id'))
        if identity in identities:
            raise ValueError('Duplicate sequential focus ID')
        identities.add(identity)
        if not isinstance(focus.get('label'), str) or not focus['label'].strip():
            raise ValueError('Sequential focus requires a label')
        focus_position(config, focus)


def initial_progress(config):
    settings = config.get('sequential_study')
    if not settings:
        return None
    return {'index': 0, 'done': False, 'target_black_streak': settings['target_black_streak'],
            'ordering': 'game_commit_order',
            'tasks': [dict(id=f['id'], label=f['label'], status='running' if i == 0 else 'pending',
                           attempts=0, completed=0, interrupted=0, results={}, research_results={},
                           black_streak=0, best_black_streak=0, last_game_id=None)
                      for i, f in enumerate(settings['focuses'])]}


def progress(config, service):
    return service.get('sequential_study') or initial_progress(config)


def can_dispatch(config, service, in_flight=0):
    state = progress(config, service)
    if state is None:
        return True
    if state['done']:
        return False
    current = state['tasks'][state['index']]
    from .study_plan import default_plan
    plan = service.get('study_plan', default_plan(config))
    if plan['focus']['id'] != current['id']:
        raise ValueError('Sequential focus differs from the current task')
    return current['black_streak'] + in_flight < state['target_black_streak']


def completed(config, service):
    state = progress(config, service)
    return bool(state and state['done'])


def record_game(config, service, game):
    """Return the next service projection; unfinished games never earn a win."""
    state = progress(config, service)
    if state is None:
        return None
    state = deepcopy(state)
    if state['done']:
        raise ValueError('Game finished after all sequential tasks were completed')
    task = state['tasks'][state['index']]
    if game.get('focus', {}).get('id') != task['id']:
        raise ValueError('Game belongs to another sequential task')
    task['attempts'] += 1
    task['last_game_id'] = game['game_id']
    if game['completed']:
        task['completed'] += 1
        result = game.get('rule_result', 'unresolved')
        research = game.get('research_result', 'unresolved')
        task['results'][result] = task['results'].get(result, 0) + 1
        task['research_results'][research] = task['research_results'].get(research, 0) + 1
        task['black_streak'] = task['black_streak'] + 1 if result == 'black_win' else 0
        task['best_black_streak'] = max(task['best_black_streak'], task['black_streak'])
    else:
        task['interrupted'] += 1
    next_service = deepcopy(service)
    next_service['sequential_study'] = state
    if task['black_streak'] >= state['target_black_streak']:
        advance(config,next_service,game['game_id'])
    return next_service


def advance(config,service,game_id,criterion_task_id=None):
    state=service['sequential_study']; task=state['tasks'][state['index']]
    task.update(status='verified',verified_game_id=game_id,verified_unix=time.time())
    if criterion_task_id: task['verified_criterion_task_id']=criterion_task_id
    state['index']+=1
    if state['index']==len(state['tasks']): state['done']=True
    else:
        from .study_plan import default_plan
        state['tasks'][state['index']]['status']='running'
        plan=deepcopy(service.get('study_plan',default_plan(config)))
        plan['focus']=deepcopy(config['sequential_study']['focuses'][state['index']])
        plan['revision']='sequential-'+(criterion_task_id or game_id)
        service['study_plan']=plan
