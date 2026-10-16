"""Private, checksummed game spool. GUI projections are never recovery inputs."""
from hashlib import sha256
import json
from pathlib import Path
import time
from .store import atomic_write, canonical


class DurableGames:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.directory = self.runtime / 'durable'
        self.cleanup_pending = set()
        self.cleanup_error = None

    def path(self, game_id, kind):
        from .tasks import valid_id
        valid_id(game_id)
        return self.directory / (game_id + '.' + kind + '.json')

    def save(self, game, kind):
        payload = canonical(game)
        wrapper = {'schema':1, 'sha256':sha256(payload.encode()).hexdigest(), 'game':game}
        atomic_write(self.path(game['game_id'],kind),canonical(wrapper)+chr(10))

    def checkpoint(self, game):
        self.save(game,'checkpoint')

    def result(self, game):
        self.save(game,'result')

    @staticmethod
    def read(path):
        wrapper=json.loads(Path(path).read_text(encoding='utf-8'))
        if wrapper.get('schema')!=1 or sha256(canonical(wrapper['game']).encode()).hexdigest()!=wrapper.get('sha256'):
            raise ValueError('Invalid durable game checksum: '+str(path))
        return wrapper['game']

    def records(self, committed=None):
        # Final records take precedence over earlier unfinished checkpoints.
        records={}
        for path in sorted(self.directory.glob('*.checkpoint.json')):
            identity=path.name.rsplit('.',2)[0]
            if committed and committed(identity):
                self.retire(identity);continue
            game=self.read(path)
            if game['game_id']!=identity:raise ValueError('Durable game filename differs from record')
            records[game['game_id']]=(game,False)
        for path in sorted(self.directory.glob('*.result.json')):
            identity=path.name.rsplit('.',2)[0]
            if committed and committed(identity):
                self.retire(identity);continue
            game=self.read(path)
            if game['game_id']!=identity:raise ValueError('Durable game filename differs from record')
            records[game['game_id']]=(game,True)
        return sorted(records.values(),key=lambda r:(r[0].get('finished_unix',r[0].get('started_unix',0)),r[0]['game_id']))

    def retire(self, game_id):
        for kind in ('checkpoint','result'):
            self.cleanup_pending.add(self.path(game_id,kind))
        self.cleanup()

    def cleanup(self, limit=16):
        self.cleanup_error=None
        for path in list(self.cleanup_pending)[:limit]:
            try:
                # Deletion is housekeeping. Never block commits or new games.
                path.unlink(missing_ok=True)
                self.cleanup_pending.discard(path)
            except OSError as error:
                self.cleanup_error=str(error)


def validate_record(game, config):
    from .runner import position_board,repetition
    import cshogi
    from .learning import key_of
    repetition_limit=game.get('settings',{}).get('repetition_limit',4)
    if 'settings' in game:
        from .live_settings import validate_changes,effective_config
        validate_changes(config,game['settings'],check_paths=False)
        if type(game.get('settings_revision')) is not int or game['settings_revision']<0:
            raise ValueError('Invalid durable settings revision')
        config=effective_config(config,game['settings'])
    if game.get('campaign_id')!=config['campaign_id']:
        raise ValueError('Durable game belongs to another campaign')
    if type(game.get('completed')) is not bool or game.get('research_result') not in ('black_win','white_win','draw','unresolved'):
        raise ValueError('Invalid durable game outcome')
    anchor,origin,anchor_prefix,*_=position_board(config['start_position'])
    if 'focus' in game:
        from .study_plan import focus_position
        base,_,prefix,keys,checks=position_board(focus_position(config,game['focus']))
        if game.get('tree_prefix_length')!=len(anchor_prefix):
            raise ValueError('Durable tree anchor differs from campaign')
    else:
        base,origin,prefix,keys,checks=position_board(config['start_position'])
    moves=game.get('moves',[])
    if game.get('origin')!=origin or game.get('prefix_length')!=len(prefix) or moves[:len(prefix)]!=prefix:
        raise ValueError('Durable game start differs from campaign')
    if len(moves)>config['max_total_plies'] or len(game.get('selections',[]))!=len(moves)-len(prefix):
        raise ValueError('Invalid durable game length')
    if repetition_limit==2:
        keys,checks=[key_of(base)],[]
        if game.get('repetition_history_start_ply',len(prefix))!=len(prefix):
            raise ValueError('Invalid study repetition history start')
    for move,item in zip(moves[len(prefix):],game['selections']):
        if repetition_limit==2 and repetition(keys,checks,2):
            raise ValueError('Durable game continued after first repeated study position')
        if item.get('key')!=key_of(base) or item.get('move')!=move or item.get('source') not in ('engine','book','fixed'):
            raise ValueError('Durable selection does not match legal game')
        parsed=base.move_from_usi(move)
        if not base.is_legal(parsed):raise ValueError('Illegal durable move: '+move)
        side='b' if base.turn==cshogi.BLACK else 'w'
        base.push(parsed)
        keys.append(key_of(base));checks.append((side,base.is_check()))
    if game.get('total_plies')!=base.move_number-1:
        raise ValueError('Durable total plies differs from legal game')
    if base.move_number-1>config['max_total_plies']:
        raise ValueError('Durable game exceeds total-ply limit')
    if not game['completed'] and game['research_result']!='unresolved':
        raise ValueError('Unfinished game cannot have a winner')
    if game['completed']:
        reason=game.get('reason');rule=game.get('rule_result');research=game['research_result']
        side='b' if base.turn==cshogi.BLACK else 'w'
        opponent='white_win' if side=='b' else 'black_win'
        expected=None
        if reason=='resign' and game.get('terminal')=='resign':expected=opponent
        elif reason=='declaration' and game.get('terminal')=='win' and base.is_nyugyoku():
            expected='black_win' if side=='b' else 'white_win'
        elif reason=='mate' and base.is_game_over():expected=opponent
        elif reason=='max_total_plies' and base.move_number-1>=config['max_total_plies']:
            if rule!='unresolved' or research!=config['research_outcomes']['max_total_plies']:
                raise ValueError('Invalid durable maximum-ply adjudication')
            return
        elif reason in ('repetition','perpetual_check','loop_abort'):
            end=repetition(keys,checks,repetition_limit)
            if reason=='loop_abort':
                if repetition_limit!=2 or end!=('repetition','draw') or rule!='unresolved':
                    raise ValueError('Invalid durable early-loop adjudication')
            elif not end or end[0]!=reason or rule!=end[1]:raise ValueError('Invalid durable repetition')
            expected_research=config['research_outcomes']['repetition'] if reason in ('repetition','loop_abort') else end[1]
            if research!=expected_research:raise ValueError('Invalid durable repetition outcome')
            return
        if expected is None or rule!=expected or research!=expected:
            raise ValueError('Invalid durable terminal adjudication')
    elif game.get('rule_result')!='unresolved':
        raise ValueError('Unfinished game cannot have a rule winner')
