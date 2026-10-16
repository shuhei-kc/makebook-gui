"""One-time original-book rebuild, executed by the sole manager before dispatch.

Raw games, results, sequential progress and runtime settings are never rewritten.
An explicit request in this campaign's maintenance directory is required.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from .learning import legacy_learn, candidate_id
from .store import atomic_write, canonical


def replay_original(events):
    book, completed = {}, 0
    for event in events:
        payload = event['payload']
        game = payload.get('game')
        # Explicit candidate inputs remain explicit inputs, not invented wins.
        if not game and event['kind'] != 'learning_restored':
            for row in payload.get('upserts',{}).values():
                if row['active']:
                    book.setdefault(row['key'],{})[row['move']] = row['wins']
                else:
                    book.get(row['key'],{}).pop(row['move'],None)
        if game and game.get('completed'):
            completed += 1
            result = game.get('research_result')
            if result in ('black_win','white_win'):
                loser = 'w' if result == 'black_win' else 'b'
                legacy_learn(book, game['selections'], loser)
    return {key:moves for key,moves in book.items() if moves}, completed


def active_book(state):
    book = {}
    for row in state.values():
        if row['active']:
            book.setdefault(row['key'],{})[row['move']] = row['wins']
    return book


def apply_requested_restore(config,store):
    path = Path(config['runtime_state_dir'])/'maintenance/restore-legacy-request.json'
    if not path.exists():
        return None
    request = json.loads(path.read_text(encoding='utf-8'))
    if (request.get('schema') != 1 or request.get('campaign_id') != config['campaign_id']
            or request.get('policy') != 'legacy_backtrack' or not request.get('request_id')
            or request.get('replay_completed_games') is not True):
        raise ValueError('Invalid original-learning restore request')
    service = store.service()
    prior = service.get('learning_restore',{})
    if prior.get('request_id') == request['request_id']:
        if service.get('learning_policy') != 'legacy_backtrack':
            raise ValueError('Restore record and learning policy differ')
        return prior
    if service.get('learning_policy','generation_guard') != 'generation_guard':
        raise ValueError('Refuse a second different original-learning migration')
    events = [json.loads(row[0]) for row in store.db.execute('SELECT payload FROM events ORDER BY seq')]
    book, completed = replay_original(events)
    before = store.candidates()
    rebuilt = deepcopy(before)
    desired = {candidate_id(key,move):(key,move,count)
               for key,moves in book.items() for move,count in moves.items()}
    if set(desired)-set(rebuilt):
        raise ValueError('Replayed book contains candidates missing from journal projection')
    restored, removed = [], []
    for identity,row in rebuilt.items():
        if identity in desired:
            count = desired[identity][2]
            if not row['active']:
                row.update(generation=row['generation']+1,reason='legacy_replay_restore')
                row.pop('disabled_by',None)
                restored.append(identity)
            row.update(active=True,wins=count)
        elif row['active']:
            row.update(active=False,reason='legacy_replay_removed')
            removed.append(identity)
    if active_book(rebuilt) != book:
        raise ValueError('Original book reconstruction differs from candidate projection')
    changes = {identity:row for identity,row in rebuilt.items() if row != before[identity]}
    source_seq = store.current_seq()
    directory = path.parent/'restore-legacy-backup'
    directory.mkdir(exist_ok=True)
    backup = directory/'before.sqlite3'
    if not backup.exists():
        target = sqlite3.connect(backup)
        try:
            store.db.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Pre-restore backup integrity failed')
        finally:
            target.close()
    info = {'request_id':request['request_id'],'policy':'legacy_backtrack',
            'source_seq':source_seq,'completed_games_replayed':completed,
            'restored_candidates':restored,'removed_candidates':removed,
            'changed_candidates':len(changes),'active_moves':len(desired),
            'active_positions':len(book),'book_sha256':sha256(canonical(book).encode()).hexdigest(),
            'applied_unix':time.time(),'backup':str(backup),
            'games_results_streak_settings_unchanged':True}
    new_service = service | {'learning_policy':'legacy_backtrack','learning_restore':info}
    store.commit('learning_restored',{'upserts':changes,'service':new_service,
                 'learning_restore':info},'learning-restore:'+request['request_id'])
    # This is an optional report. The committed event is the recovery authority.
    try:
        atomic_write(path.parent/'restore-legacy-result.json',canonical(info)+'\n')
    except OSError:
        pass
    return info
