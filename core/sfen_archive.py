"""Continuously publish every real game as a standard one-line USI SFEN record.

Completed and current games retain their full legal startpos
history. A separate index supplies the game ID, actual study start, and outcome.
Published files are disposable projections; SQLite and the private spool remain
the recovery inputs. Only the campaign manager writes this archive.
"""
import json
from pathlib import Path
import time
from .store import atomic_write


def game_line(game):
    origin = game.get('origin', 'startpos')
    moves = game.get('moves', [])
    if '\n' in origin or '\r' in origin or any(not isinstance(m, str) or any(c.isspace() for c in m) for m in moves):
        raise ValueError('Invalid SFEN export record')
    return origin + (' moves ' + ' '.join(moves) if moves else '')


class SfenArchive:
    def __init__(self, config):
        self.config = config
        self.path = Path(config['sfen_archive']) if config.get('sfen_archive') else None
        self.signature = None
        self.info = {}

    def publish(self, store):
        if self.path is None:
            return {}
        runtime = Path(self.config['runtime_state_dir'])
        paths = [runtime / 'in_progress.json']
        paths += sorted((runtime / 'workers').glob('*/in_progress.json'))
        live = []
        for path in paths:
            try:
                game = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                continue
            if game.get('campaign_id') != self.config['campaign_id']:
                raise ValueError('SFEN checkpoint belongs to another campaign')
            if not store.game_exists(game['game_id']):
                live.append(game)
        last = store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM games').fetchone()[0]
        signature = (last, tuple((g['game_id'], len(g['moves'])) for g in live))
        index_path = self.path.with_suffix('.index.json')
        if signature == self.signature and self.path.exists() and index_path.exists():
            return dict(self.info)
        games = [(json.loads(row[0]), False) for row in store.db.execute(
            "SELECT payload FROM games WHERE json_extract(payload,'$.completed')=1 ORDER BY rowid")]
        games += [(game, True) for game in live]
        lines = [game_line(game) for game, _ in games]
        from hashlib import sha256
        content = ''.join(line + '\n' for line in lines)
        index = {'schema_version': 1, 'campaign_id': self.config['campaign_id'], 'updated_unix': time.time(),
                 'sfen_sha256': sha256(content.encode('utf-8')).hexdigest(), 'games': []}
        for number, (game, playing) in enumerate(games, 1):
            index['games'].append(dict(line=number, game_id=game['game_id'],
                                       focus=game.get('focus'), start_ply=game.get('prefix_length'),
                                       playing=playing, completed=game.get('completed', False),
                                       rule_result=game.get('rule_result', 'unresolved'),
                                       research_result=game.get('research_result', 'unresolved'),
                                       reason=game.get('reason'), plan_revision=game.get('plan_revision'),
                                       settings_revision=game.get('settings_revision'),settings=game.get('settings')))
        atomic_write(self.path, content)
        atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2) + '\n')
        self.info = dict(path=str(self.path), index_path=str(index_path), games=len(games),
                         playing=len(live), updated_unix=index['updated_unix'])
        self.signature = signature
        return dict(self.info)
