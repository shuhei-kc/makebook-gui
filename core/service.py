"""Service boundary: failures stay visible and restart into a persisted pause."""
import contextlib,json,os,time,traceback,uuid
from pathlib import Path
from .store import Store,ManagerLock,atomic_write,canonical
from .runtime_log import SafeLog

_process_job=None

def read_published_status(runtime, campaign_id=None):
    records=[]
    for name in ('status.json', 'status-live.json'):
        try:
            value=json.loads((Path(runtime)/name).read_text(encoding='utf-8'))
            owner=value.get('campaign_id') if isinstance(value,dict) else None
            # Legacy primary snapshots predate the owner field. The alternate
            # publication must always identify this campaign.
            if isinstance(value,dict) and (campaign_id is None or owner==campaign_id or
                    (name=='status.json' and owner is None)):
                records.append(value)
        except (OSError,ValueError):
            pass
    if not records:
        raise FileNotFoundError('No valid published status')
    return max(records,key=lambda value:value.get('updated_unix',0))

def merge_health(runtime,status):
    try:
        health=json.loads((Path(runtime)/'health.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return status
    if health.get('fatal_exit') and health.get('updated_unix',0)>=status.get('updated_unix',0):
        return status|health|{'active_workers':0,'current_game_id':None,'workers':[]}
    return status

def serve_campaign(config,max_games,run=None,own_process=False):
    global _process_job
    runtime=Path(config['runtime_state_dir']);runtime.mkdir(parents=True,exist_ok=True)
    sink=SafeLog(runtime/'manager.log')
    def health(**fields):
        value={'campaign_id':config['campaign_id'],'manager_pid':os.getpid(),'updated_unix':time.time()}|fields
        try:atomic_write(runtime/'health.json',canonical(value)+'\n')
        except OSError as error:sink.write('Health publication failed: '+str(error)+'\n')
    if run is None:
        from .runner import run_campaign
        run=run_campaign
    with contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
        try:
            if own_process and os.name=='nt' and _process_job is None:
                # Keep the handle until process exit. Children inherit membership,
                # including the interval before their individual job assignment.
                import ctypes
                from types import SimpleNamespace
                from .process_job import ProcessJob
                _process_job=ProcessJob()
                _process_job.attach(SimpleNamespace(_handle=-1))
            print(canonical({'event':'manager_started','time':time.time()}),flush=True)
            finished,attempts,stopped=run(config,max_games)
            health(phase='stopped',fatal_exit=False,fault=None)
            print(canonical({'event':'manager_stopped','time':time.time(),'finished':finished,'attempts':attempts}),flush=True)
            return 0 if stopped or max_games is None or finished==max_games else 1
        except Exception as error:
            # A second launch must not fault or pause the existing manager.
            if isinstance(error,RuntimeError) and str(error)=='Another manager is already running':
                print('Another manager is already running',flush=True);return 0
            details=''.join(traceback.format_exception(type(error),error,error.__traceback__))
            sink.write(details)
            fault=type(error).__name__+': '+str(error)
            health(phase='faulted',fatal_exit=True,paused=True,fault=fault)
            try:
                with ManagerLock(runtime):
                    store=Store(runtime/'state.sqlite3')
                    try:
                        store.commit('service_fault',{'service':store.service() | {'paused':True,'fault':fault}},'fault:'+uuid.uuid4().hex)
                        store.replicate(runtime/'recovery'/'events.jsonl')
                    finally:store.close()
            except Exception as persistence_error:
                sink.write('Fault persistence failed: '+repr(persistence_error)+'\n')
            # Persisted pause prevents an external process supervisor from
            # new games after an unexpected failure; damaged recovery fails closed.
            return 1
