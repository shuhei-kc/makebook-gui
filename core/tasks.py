"""Durable file mailbox for controls handled by the independent manager."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .learning import Candidates, candidate_id
from .store import atomic_write, canonical


OPERATIONS = {"pause", "resume", "stop_after_games", "abort_now", "status", "dump", "set_streak_target", "configure"}
LOCATION_KEYS = {"runtime_state_dir", "queue_dir", "result_dir",
                 "start_paused", "poll_seconds", "status_seconds", "workers"}


def study_config(config):
    return {key: value for key, value in config.items() if key not in LOCATION_KEYS}


def config_revision(config):
    return hashlib.sha256(canonical(study_config(config)).encode()).hexdigest()


def valid_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            or value.endswith(".") or value.split(".")[0].upper() in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError("Invalid task/campaign ID")
    return value


def validate_request(request, config):
    if not isinstance(request, dict) or type(request.get("schema_version")) is not int or request.get("schema_version") != 1:
        raise ValueError("Expected task schema_version=1")
    valid_id(request.get("task_id"))
    target = request.get("target")
    if not isinstance(target, dict) or target.get("campaign_id") != config["campaign_id"]:
        raise ValueError("Task targets another campaign")
    revision = target.get("config_revision", "at_acceptance")
    if revision not in ("at_acceptance", config_revision(config)):
        raise ValueError("Task targets another configuration revision")
    priority = request.get("priority", 0)
    if type(priority) is not int or not -1000 <= priority <= 1000:
        raise ValueError("priority must be an integer from -1000 to 1000")
    if request.get("kind") == "control":
        if request.get("operation") not in OPERATIONS:
            raise ValueError("Unsupported operation")
        if request["operation"] == "set_streak_target":
            if not config.get('sequential_study'):
                raise ValueError('This campaign has no sequential study')
            for name in ('target_black_streak', 'expected_target_black_streak'):
                if type(request.get(name)) is not int or request[name] < 1:
                    raise ValueError(name + ' must be a positive integer')
        if request['operation']=='configure':
            from .live_settings import validate_changes
            if type(request.get('expected_settings_revision')) is not int or request['expected_settings_revision']<0:
                raise ValueError('expected_settings_revision is required')
            validate_changes(config,request.get('changes'))
    elif request.get("kind") == "add_candidates":
        if request.get("activation", "preserve") not in ("preserve", "reactivate"):
            raise ValueError("Invalid activation")
        variations = request.get("variations")
        if not isinstance(variations, list) or not 1 <= len(variations) <= 256:
            raise ValueError("Expected 1..256 variations")
    elif request.get("kind") == "study_plan":
        if not isinstance(request.get("expected_plan_revision"), str):
            raise ValueError("Study plan requires its expected revision")
    else:
        raise ValueError("Supported kinds: add_candidates, control, study_plan")


def plan_candidates(request, config, state):
    # Validate ALL variations before committing ANY row or candidate.
    from .runner import position_board
    from .learning import key_of
    book = Candidates(state)
    registered, line_ids = {}, set()
    for index, line in enumerate(request["variations"]):
        if not isinstance(line, dict) or line.get("start", "campaign_start") != "campaign_start":
            raise ValueError("Variation must start at campaign_start")
        identity = valid_id(line.get("line_id", f"line-{index + 1}"))
        if identity in line_ids:
            raise ValueError("Duplicate line_id")
        line_ids.add(identity)
        moves = line.get("moves")
        if not isinstance(moves, list) or not moves or not all(isinstance(move, str) for move in moves):
            raise ValueError("Expected a nonempty list of USI moves")
        board, *_ = position_board(config["start_position"])
        if board.move_number - 1 + len(moves) > config["max_total_plies"]:
            raise ValueError("Variation exceeds campaign total-ply limit")
        registration = line.get("registration")
        if not isinstance(registration, dict):
            raise ValueError("Explicit registration scope is required")
        if registration.get("scope") == "all_edges":
            plies = set(range(1, len(moves) + 1))
        elif registration.get("scope") == "selected_plies":
            values = registration.get("plies")
            if (not isinstance(values, list) or not values or
                    any(type(value) is not int or not 1 <= value <= len(moves) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Invalid selected_plies")
            plies = set(values)
        else:
            raise ValueError("Unsupported registration scope")
        context_length=line.get('context_length',0)
        if type(context_length) is not int or not 0 <= context_length < len(moves) or any(p <= context_length for p in plies):
            raise ValueError('Context plies cannot be registered as candidates')
        for ply, move in enumerate(moves, 1):
            key = key_of(board)
            if move in ("resign", "win", "rep_draw", "max_moves"):
                raise ValueError("Terminal tokens cannot be registered")
            parsed = board.move_from_usi(move)
            if not board.is_legal(parsed):
                raise ValueError(f"{identity}: illegal move at relative ply {ply}: {move}")
            fixed = next((rule for rule in config.get("fixed_rules", [])
                          if rule["canonical_sfen"] == key), None)
            if ply > context_length and fixed and move not in {item["usi"] for item in fixed["moves"]}:
                raise ValueError(f"{identity}: move contradicts fixed rule")
            if ply in plies:
                row = book.register(key, move, request.get("activation", "preserve") == "reactivate")
                source = {"task_id": request["task_id"], "line_id": identity, "relative_ply": ply,
                          "source": deepcopy(line.get("source", {})),
                          "description": line.get("description", ""), "tags": line.get("tags", [])}
                row.setdefault("sources", [])
                if source not in row["sources"]:
                    row["sources"].append(source)
                registered[row["id"]] = {"id": row["id"], "generation": row["generation"]}
            board.push(parsed)
    changes = {identity: row for identity, row in book.state.items()
               if row != state.get(identity)}
    return book, changes, list(registered.values())


def submit(config, request):
    validate_request(request, config)
    inbox = Path(config.get("queue_dir", str(Path(config["runtime_state_dir"]) / "tasks"))) / "inbox"
    destination = inbox / (request["task_id"] + "-" + uuid.uuid4().hex + ".task.json")
    atomic_write(destination, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    return destination


class Controller:
    def __init__(self, config, store, runner):
        self.config, self.store, self.runner = config, store, runner
        self.queue = Path(config.get("queue_dir", str(runner.runtime / "tasks")))
        self.results = Path(config.get("result_dir", str(runner.runtime / "results")))
        for name in ("inbox", "archive", "rejected"):
            (self.queue / name).mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.last_poll = 0
        self.poll_error = None
        self.output_error = None
        self.written_results = {}

    def initialize(self):
        store, config = self.store, self.config
        if store.db.execute("SELECT 1 FROM metadata WHERE key='service'").fetchone() is None:
            service = {"paused": bool(config.get("start_paused", False)), "fault": None, "learning_policy": "legacy_backtrack"}
            if config.get('sequential_study'):
                from .study_plan import default_plan
                from .sequential_study import initial_progress
                service.update(study_plan=default_plan(config), sequential_study=initial_progress(config))
            store.commit("service_initialized",
                         {"service": service},
                         "service:" + uuid.uuid4().hex)
        elif config.get('sequential_study'):
            service = store.service()
            if 'sequential_study' not in service or 'study_plan' not in service:
                # Older service-fault handlers replaced the whole projection.
                # The last committed service in the journal retains the plan
                # and streak; recover it before any game or worker is started.
                row = store.db.execute("SELECT payload FROM events "
                    "WHERE json_type(payload,'$.payload.service.sequential_study')='object' "
                    "ORDER BY seq DESC LIMIT 1").fetchone()
                if row is None:
                    if store.status()['attempts']:
                        raise ValueError('Sequential progress is missing from the recovery journal')
                    from .study_plan import default_plan
                    from .sequential_study import initial_progress
                    saved = dict(study_plan=default_plan(config), sequential_study=initial_progress(config))
                else:
                    saved = json.loads(row[0])['payload']['service']
                state = saved['sequential_study']
                changed = saved.get('sequential_target_change', {})
                if ([t['id'] for t in state['tasks']] != [f['id'] for f in config['sequential_study']['focuses']] or
                        (state['target_black_streak'] != config['sequential_study']['target_black_streak'] and
                         changed.get('to') != state['target_black_streak'])):
                    raise ValueError('Recovered sequential progress differs from this campaign')
                service.update(study_plan=deepcopy(saved['study_plan']), sequential_study=deepcopy(state))
                if changed:
                    service['sequential_target_change'] = deepcopy(changed)
                store.commit('sequential_state_recovered', {'service':service}, 'recover-sequence:' + uuid.uuid4().hex)
        # Older fault handlers could lose the projection, but the journal
        # retains the accepted overrides. Never fall back silently to baseline.
        if 'runtime_settings' not in store.service():
            row=store.db.execute("SELECT payload FROM events WHERE "
                "json_type(payload,'$.payload.service.runtime_settings')='object' ORDER BY seq DESC LIMIT 1").fetchone()
            if row:
                saved=json.loads(row[0])['payload']['service']['runtime_settings']
                store.commit('settings_recovered',{'service':store.service() | {'runtime_settings':saved}},
                             'recover-settings:'+uuid.uuid4().hex)

    @property
    def paused(self):
        return self.store.service()["paused"]

    def accept(self, request, raw_hash):
        validate_request(request, self.config)
        request_hash = hashlib.sha256(canonical(request).encode()).hexdigest()
        existing = self.store.task(request["task_id"])
        if existing:
            if existing["request_hash"] != request_hash:
                raise ValueError("Same task_id has different content; use a new ID")
            return existing
        task = {"task_id": request["task_id"], "kind": request["kind"], "state": "accepted",
                "request": deepcopy(request), "request_hash": request_hash,
                "file_hash": raw_hash, "accepted_unix": time.time(),
                "config_revision": config_revision(self.config)}
        changes = {}
        payload = {"task": task, "upserts": changes}
        if request["kind"] == "add_candidates":
            from .study_plan import default_plan
            service = self.store.service()
            effective = self.config | {"fixed_rules": service.get("study_plan", default_plan(self.config))["fixed_rules"]}
            book, changes, registered = plan_candidates(request, effective, self.runner.book.state)
            task["candidates"] = registered
            payload["upserts"] = changes
        elif request["kind"] == "control" and request["operation"] == 'set_streak_target':
            service = self.store.service()
            state = deepcopy(service['sequential_study'])
            old_target = state['target_black_streak']
            new_target = request['target_black_streak']
            if old_target != request['expected_target_black_streak']:
                raise ValueError('Streak target changed; reload it before updating')
            if state['done']:
                raise ValueError('All sequential tasks have already completed')
            current = state['tasks'][state['index']]
            # Reserve a possible win for EVERY outstanding game, including
            # completed futures whose results have not yet been committed.
            # Reject an unsafe reduction instead of advancing the focus early.
            in_flight = len(self.runner.futures) if hasattr(self.runner, 'futures') else int(bool(self.runner.current_game))
            if new_target <= current['black_streak'] or new_target < current['black_streak'] + in_flight:
                raise ValueError('Target is below the current streak and outstanding game reservations')
            state['target_black_streak'] = new_target
            change = {'task_id':request['task_id'], 'from':old_target, 'to':new_target,
                      'changed_unix':time.time()}
            payload['service'] = service | {'sequential_study':state, 'sequential_target_change':change}
            from .live_settings import current
            setting=current(self.config,service)
            if setting['values']['target_black_streak']!=old_target:
                raise ValueError('A settings change is pending; use configure with its latest revision')
            setting.update(revision=setting['revision']+1,task_id=request['task_id'],accepted_unix=time.time())
            setting['values']['target_black_streak']=new_target
            payload['service']['runtime_settings']=setting
            task.update(state='completed', applied_unix=change['changed_unix'], response=change)
        elif request['kind']=='control' and request['operation']=='configure':
            from .live_settings import configure
            service=self.store.service()
            setting=configure(self.config,service,request)
            previous=service.get('runtime_settings',{}).get('task_id')
            if previous:
                prior=self.store.task(previous)
                if prior and prior.get('state')=='waiting_for_settings':
                    prior.update(state='superseded',superseded_by=request['task_id'])
                    # Both requests and the new desired configuration are
                    # committed together; a superseded request cannot reapply.
                    payload['tasks']=[prior]
            payload['service']=service | {'runtime_settings':setting}
            task.update(state='waiting_for_settings',response=setting)
        elif request["kind"] == "study_plan":
            from .study_plan import default_plan, update_plan
            service = self.store.service()
            plan = update_plan(self.config, service.get("study_plan", default_plan(self.config)), request)
            payload["service"] = service | {"study_plan": plan}
            task.update(state="completed", applied_unix=time.time(), response=plan)
        try:
            self.store.commit("task_accepted", payload,
                              "task:" + task["task_id"])
        except (OSError,sqlite3.Error):
            # Resolve an uncertain COMMIT from the authoritative task identity.
            committed=self.store.task(task['task_id'])
            if committed is None or committed['request_hash']!=request_hash:
                raise
        # Reload only AFTER the transaction is known to have committed.
        self.runner.book = Candidates(self.store.candidates())
        return self.store.task(task["task_id"])

    def poll(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_poll < self.config.get("poll_seconds", 0.25):
            return
        self.last_poll = now
        before = self.store.current_seq()
        try:
            self.execute_pending()
            files = list((self.queue / "inbox").glob("*.task.json"))
            pending = []
            for path in sorted(files)[:64]:
                try:
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError("Task file exceeds 1MiB")
                    raw = path.read_bytes()
                    if len(raw) > 1024 * 1024:
                        raise ValueError("Task file exceeds 1MiB")
                    request = json.loads(raw.decode("utf-8-sig"))
                    priority = request.get("priority", 0) if isinstance(request, dict) else 0
                    pending.append((-(priority if type(priority) is int else 0), path, request, raw))
                except (ValueError, UnicodeError) as error:
                    self.reject(path, str(error))
            for _, path, request, raw in sorted(pending, key=lambda item: (item[0], item[1].name))[:16]:
                try:
                    self.accept(request, hashlib.sha256(raw).hexdigest())
                except (ValueError, KeyError, TypeError, OverflowError) as error:
                    self.reject(path, str(error), request)
                    continue
                self.publish()
                os.replace(path, self.queue / "archive" /
                           (hashlib.sha256(raw).hexdigest() + "-" + uuid.uuid4().hex[:8] + ".task.json"))
            self.execute_pending()
            self.poll_error = None
        except (OSError, sqlite3.Error) as error:
            # A transient share or database-write failure is not a rejected task.
            self.poll_error = str(error)
        finally:
            if self.store.current_seq() != before:
                self.runner.publish()

    def reject(self, path, error, request=None):
        token = uuid.uuid4().hex
        result = {"input_file": path.name, "state": "rejected", "error": error}
        identity = request.get("task_id") if isinstance(request, dict) else None
        try:
            valid_id(identity)
        except ValueError:
            identity = None
        if identity:
            result["task_id"] = identity
            if self.store.task(identity) is None:
                atomic_write(self.results / (identity + ".json"), canonical(result) + "\n")
        atomic_write(self.queue / "rejected" / (token + ".result.json"), canonical(result) + "\n")
        os.replace(path, self.queue / "rejected" / (token + ".task.json"))

    def execute_pending(self):
        for task in self.store.pending_controls():
            if task["kind"] != "control" or task["state"] not in ("accepted", "waiting_for_game"):
                continue
            operation = task["request"]["operation"]
            if operation == "stop_after_games":
                self.runner.stop_after = True
            elif operation == "abort_now":
                self.runner.abort = self.runner.stop_after = True
            if task["state"] == "waiting_for_game":
                continue
            payload = {}
            if operation in ("pause", "resume"):
                payload["service"] = self.store.service() | {"paused": operation == "pause", "fault": None}
                if operation == "resume":
                    self.runner.consecutive_errors = 0
            if operation == "status":
                task["response"] = self.runner.publish(full=False)
            task["state"] = ("waiting_for_game" if operation in ("stop_after_games", "abort_now")
                             and self.runner.current_game else "completed")
            task["applied_unix"] = time.time()
            payload["task"] = task
            self.store.commit("control_applied", payload, "apply:" + task["task_id"])
            if operation == "dump":
                self.runner.publish()
            self.publish()

    def finish_stops(self, game_id=None):
        for task in self.store.pending_controls():
            if task["kind"] == "control" and task["state"] == "waiting_for_game":
                task.update(state="completed", finished_unix=time.time(), final_game_id=game_id)
                self.store.commit("control_finished", {"task": task}, "finish:" + task["task_id"])
        self.publish()

    def pause_fault(self, error):
        self.store.commit("service_fault",
                          {"service": self.store.service() | {"paused": True, "fault": str(error)}},
                          "fault:" + uuid.uuid4().hex)
        self.publish()

    def publish(self):
        self.output_error = None
        for task in self.store.tasks():
            result = deepcopy(task)
            if task["kind"] == "add_candidates":
                progress = []
                for registered in task["candidates"]:
                    record = self.store.db.execute("SELECT payload FROM candidates WHERE id=?", (registered['id'],)).fetchone()
                    row = json.loads(record[0])
                    reached, verified = self.store.task_progress(registered['id'],task['accepted_seq'],registered['generation'])
                    progress.append({"id": row["id"], "key": row["key"], "move": row["move"],
                                     "active": row["active"], "generation": row["generation"],
                                     "wins": row["wins"], "reached_games": reached,
                                     "verified_games": verified,
                                     "disabled_by": row.get("disabled_by"), "reason": row["reason"]})
                result["state"] = "registered"
                result["progress"] = progress
            serialized = json.dumps(result,ensure_ascii=False,indent=2)+"\n"
            path = self.results / (task['task_id']+'.json')
            if self.written_results.get(task['task_id'])==serialized and path.is_file():
                continue
            try:
                atomic_write(path,serialized)
                self.written_results[task['task_id']]=serialized
            except OSError as error:
                self.output_error=str(error)
