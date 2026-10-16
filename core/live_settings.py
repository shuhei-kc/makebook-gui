"""Durable runtime overrides; the campaign file remains the recovery baseline."""
from copy import deepcopy
from pathlib import Path
import time

LIMITS = {'workers':(1,8), 'nodes':(1,10**12), 'threads':(1,256),
          'hash_mb':(1,1048576), 'resign_value':(0,100000),
          'max_total_plies':(1,10000), 'target_black_streak':(1,10**9), 'repetition_limit':(2,4)}
TIMEOUTS = ('init_timeout_seconds','ready_timeout_seconds',
            'search_timeout_seconds','stop_grace_seconds')
PATHS = ('engine_executable','engine_cwd')

def baseline(config, service=None):
    engine=config['engine']; options=engine.get('usi_options',{})
    values={'workers':config.get('workers',1), 'nodes':engine['nodes'],
            'engine_executable':engine['executable'], 'engine_cwd':engine['cwd'],
            'max_total_plies':config['max_total_plies'], 'repetition_limit':config.get('repetition_limit',2)}
    for field,option in (('threads','Threads'),('hash_mb','USI_Hash'),('resign_value','ResignValue')):
        if option in options: values[field]=options[option]
    values.update({name:engine[name] for name in TIMEOUTS})
    if config.get('sequential_study'):
        values['target_black_streak']=(service or {}).get('sequential_study',{}).get(
            'target_black_streak',config['sequential_study']['target_black_streak'])
    return values

def current(config, service):
    saved=deepcopy(service.get('runtime_settings'))
    if saved:
        # Records written before this option used fourfold adjudication. Keep
        # that condition until an explicit, journalled configure request changes it.
        saved['values'].setdefault('repetition_limit',4)
        return saved
    return {'revision':0,'values':baseline(config,service),'task_id':None}

def validate_changes(config, changes, check_paths=True):
    if not isinstance(changes,dict) or not changes:
        raise ValueError('設定の変更項目がありません')
    for key,value in changes.items():
        if key in LIMITS:
            low,high=LIMITS[key]
            if type(value) is not int or not low<=value<=high:
                raise ValueError(f'{key}: {low}〜{high}の整数を指定してください')
            if key=='repetition_limit' and value not in (2,4):
                raise ValueError('repetition_limit: 2（最初の再到達）または4を指定してください')
        elif key in TIMEOUTS:
            if type(value) not in (int,float) or not 0<value<=3600:
                raise ValueError(f'{key}: 0より大きく3600以下の秒数を指定してください')
        elif key in PATHS:
            if not isinstance(value,str) or '\n' in value or '\r' in value or not Path(value).is_absolute():
                raise ValueError(f'{key}: 絶対パスを指定してください')
            if check_paths and not (Path(value).is_file() if key=='engine_executable' else Path(value).is_dir()):
                raise ValueError(f'{key}: ファイル／フォルダーが存在しません')
        else:
            raise ValueError('変更できない設定項目: '+key)
    if 'target_black_streak' in changes and not config.get('sequential_study'):
        raise ValueError('この課題には連勝条件がありません')
    if 'max_total_plies' in changes:
        from .runner import position_board
        from .study_plan import focus_position
        starts=[config['start_position']]+[focus_position(config,f) for f in
                config.get('sequential_study',{}).get('focuses',[])]
        if changes['max_total_plies']<=max(position_board(p)[0].move_number-1 for p in starts):
            raise ValueError('手数上限はすべての課題開始手数より大きくしてください')

def configure(config, service, request):
    saved=current(config,service)
    if request['expected_settings_revision']!=saved['revision']:
        raise ValueError('設定が先に変更されています。最新状態を読み直してください')
    validate_changes(config,request['changes'])
    if 'max_total_plies' in request['changes'] and service.get('study_plan'):
        from .runner import position_board
        from .study_plan import focus_position
        if request['changes']['max_total_plies']<=position_board(focus_position(config,service['study_plan']['focus']))[0].move_number-1:
            raise ValueError('手数上限は現在の課題開始手数より大きくしてください')
    values=saved['values']|deepcopy(request['changes'])
    establishing_repetition=('repetition_limit' in request['changes'] and
                             'repetition_limit' not in service.get('runtime_settings',{}).get('values',{}))
    if values==saved['values'] and not establishing_repetition:
        raise ValueError('現在の設定と同じです')
    state=service.get('sequential_study')
    if state and state['done'] and 'target_black_streak' in request['changes']:
        raise ValueError('全課題が完了しています')
    return dict(revision=saved['revision']+1,values=values,task_id=request['task_id'],
                accepted_unix=time.time())

def effective_config(config, values):
    result=deepcopy(config)
    result['workers']=values['workers']; result['max_total_plies']=values['max_total_plies']
    result['repetition_limit']=values.get('repetition_limit',4)
    engine=result['engine']; engine['nodes']=values['nodes']
    engine['executable']=values['engine_executable']; engine['cwd']=values['engine_cwd']
    for key in TIMEOUTS: engine[key]=values[key]
    options=engine.setdefault('usi_options',{})
    for field,option in (('threads','Threads'),('hash_mb','USI_Hash'),('resign_value','ResignValue')):
        if field in values: options[option]=values[field]
    return result

def target_ready(config, service, outstanding):
    state=service.get('sequential_study')
    if not state or state['done']: return True
    desired=current(config,service)['values']['target_black_streak']
    streak=state['tasks'][state['index']]['black_streak']
    return desired==state['target_black_streak'] or (desired>streak and desired>=streak+outstanding) or outstanding==0

def apply_target(config, service, outstanding):
    state=service.get('sequential_study')
    if not state or state['done'] or not target_ready(config,service,outstanding): return None
    setting=current(config,service); desired=setting['values']['target_black_streak']
    if desired==state['target_black_streak']: return None
    updated=deepcopy(service); state=updated['sequential_study']; old=state['target_black_streak']
    state['target_black_streak']=desired
    updated['sequential_target_change']={'task_id':setting['task_id'],'from':old,'to':desired,'changed_unix':time.time()}
    task=state['tasks'][state['index']]
    if task['black_streak']>=desired:
        # No outstanding game remains. Changing the criterion doesn't create
        # a win; the existing official streak supplies the evidence.
        from .sequential_study import advance
        advance(config,updated,task['last_game_id'],criterion_task_id=setting['task_id'])
    return updated
