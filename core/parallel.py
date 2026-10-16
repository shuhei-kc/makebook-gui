"""Parallel games with one coordinator owning SQLite, learning and the mailbox."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import queue
import sqlite3
import threading
import time
import uuid

from .runner import Runner
from .tasks import study_config


class FrozenBook:
    """Immutable per-position snapshot shared by workers without copying the book."""

    def __init__(self, by_key):
        self.by_key = by_key

    def rows(self, key):
        return self.by_key.get(key, ())

    def active(self, key):
        return [row for row in self.rows(key) if row["active"]]

    def selection(self, key, move, source, info=None):
        row = next((r for r in self.rows(key) if r["move"] == move), None)
        return {
            "key": key,
            "move": move,
            "source": source,
            "generation": row["generation"] if row else None,
            "info": info,
        }


class WorkerView:
    """Read-only cached learner view. Worker threads never access SQLite."""

    def __init__(self, manager):
        self.manager = manager
        self.worker = None

    def candidates(self):
        # Initialization only; GameWorker.controls installs a frozen view before play.
        return {}

    def current_seq(self):
        return self.worker.book_seq

    def service(self):
        # Also supports an explicitly invoked worker in recovery/tests. No SQL.

        with self.manager.snapshot_lock:
            return {"study_plan": deepcopy(self.manager.cached_plan)}


class GameWorker(Runner):
    def __init__(self, manager, identity):
        self.manager, self.identity = manager, identity
        config = deepcopy(manager.config)
        config["runtime_state_dir"] = str(manager.runtime / "workers" / str(identity))
        view = WorkerView(manager)
        super().__init__(config, view)
        view.worker = self
        self.book_seq = -1

    def controls(self):
        with self.manager.snapshot_lock:
            if self.book_seq != self.manager.cached_seq:
                self.book = FrozenBook(self.manager.cached_by_key)
                self.book_seq = self.manager.cached_seq
        self.durable.cleanup()
        return self.manager.abort_signal.is_set()

    def checkpoint(self, game, moves, board):
        game["worker_id"] = self.identity
        super().checkpoint(game, moves, board)
        self.manager.messages.put(
            (self.identity, self.current_game, self.current_total_plies)
        )

    def publish(self, full=True):
        # Only the coordinator publishes the campaign and task results.
        return {}

    def play(self):
        self.controls()
        return super().play()


class ParallelRunner(Runner):
    def __init__(self, config, store):
        super().__init__(config, store)
        self.snapshot_lock = threading.Lock()
        from .study_plan import default_plan

        self.cached_plan = deepcopy(
            store.service().get("study_plan", default_plan(config))
        )
        self.cached_state, self.cached_seq = self.book.state, store.current_seq()
        self.cached_by_key = {
            key: tuple(deepcopy(list(rows.values())))
            for key, rows in self.book.by_key.items()
        }
        self.cache_source = self.book
        self.cache_delta = None
        self.abort_signal = threading.Event()
        self.messages = queue.Queue()
        self.workers = [
            GameWorker(self, index) for index in range(1, config["workers"] + 1)
        ]
        self.futures = {}
        self.counted_outcomes = set()
        self.activity = {
            worker.identity: {
                "worker_id": worker.identity,
                "game_id": None,
                "total_plies": None,
            }
            for worker in self.workers
        }

    def refresh_cache(self):
        seq = self.store.current_seq()
        if seq != self.cached_seq:
            with self.snapshot_lock:
                if self.cache_delta is not None and self.cache_source is self.book:
                    snapshot = dict(self.cached_by_key)
                    for key in {row["key"] for row in self.cache_delta.values()}:
                        snapshot[key] = tuple(deepcopy(list(self.book.rows(key))))
                    self.cached_by_key = snapshot
                else:
                    self.cached_by_key = {
                        key: tuple(deepcopy(list(rows.values())))
                        for key, rows in self.book.by_key.items()
                    }
                self.cached_state = self.book.state
                from .study_plan import default_plan

                self.cached_plan = deepcopy(
                    self.store.service().get("study_plan", default_plan(self.config))
                )
                self.cache_source = self.book
                self.cache_delta = None
                self.cached_seq = seq

    def collect_progress(self):
        while True:
            try:
                identity, game, plies = self.messages.get_nowait()
            except queue.Empty:
                break
            if identity in self.futures:
                self.activity[identity].update(game_id=game, total_plies=plies)
        current = next((row for row in self.activity.values() if row["game_id"]), None)
        self.current_game = (
            current["game_id"] if current else "starting" if self.futures else None
        )
        self.current_total_plies = current["total_plies"] if current else None

    def status_extra(self):
        from .live_settings import current

        setting = current(self.config, self.store.service())
        desired = setting["values"]["workers"]
        records = []
        for worker in self.workers:
            if worker.identity > desired and worker.identity not in self.futures:
                continue
            row = self.activity[worker.identity].copy()
            process = worker.engine.process
            row.update(
                phase="playing"
                if row["game_id"]
                else "starting"
                if worker.identity in self.futures
                else "idle",
                engine_pid=process.pid if process and process.poll() is None else None,
                settings_revision=worker.settings_revision,
                settings=deepcopy(worker.game_settings),
                retiring=worker.identity > desired,
            )
            records.append(row)
        pending = [
            i
            for i in self.futures
            if self.workers[i - 1].settings_revision != setting["revision"]
        ]
        return {
            "workers_count": max([desired] + list(self.futures)),
            "desired_workers": desired,
            "settings_pending_workers": pending,
            "active_workers": len(self.futures),
            "workers": records,
            "storage_blocked": self.storage_blocked,
            "io_errors": dict(self.io_errors)
            | {
                f"worker-{w.identity}-{k}": v
                for w in self.workers
                for k, v in w.io_errors.items()
            },
            "cleanup_pending": sum(
                len(w.durable.cleanup_pending) for w in self.workers
            ),
            "protocol_log_errors": {
                str(w.identity): w.engine.log_error
                for w in self.workers
                if getattr(w.engine, "log_error", None)
            },
        }

    def publish(self, full=True):
        self.collect_progress()
        self.refresh_cache()
        return super().publish(full)

    def recover_all(self):
        recovered = self.recover_runtime(self)
        # Recover all durable worker directories, including retired worker slots.
        current = {worker.identity: worker for worker in self.workers}
        paths = set((self.runtime / "workers").glob("*"))
        for path in sorted(paths):
            if not path.is_dir() or not path.name.isdigit():
                continue
            identity = int(path.name)
            worker = current.get(identity)
            if worker is None:
                from types import SimpleNamespace
                from .durable_games import DurableGames

                worker = SimpleNamespace(
                    runtime=path, durable=DurableGames(path), io_errors={}
                )
            found = self.recover_runtime(worker)
            recovered = found or recovered
        return recovered

    def failed_game(self, worker, error):
        if worker.last_game is not None:
            game = deepcopy(worker.last_game)
            # A final-spool failure does not turn a known outcome into an abort.
            if game["completed"]:
                return game
        else:
            records = worker.durable.records()
            if records:
                game = deepcopy(records[-1][0])
            else:
                from .runner import position_board
                from .study_plan import default_plan, game_start

                plan = worker.dispatched_plan or default_plan(self.config)
                board, origin, prefix, *_ = game_start(self.config, plan)
                game = {
                    "game_id": worker.current_game or str(uuid.uuid4()),
                    "campaign_id": self.config["campaign_id"],
                    "worker_id": worker.identity,
                    "moves": prefix,
                    "selections": [],
                    "tree_prefix_length": len(
                        position_board(self.config["start_position"])[2]
                    ),
                    "focus": deepcopy(plan["focus"]),
                    "plan_revision": plan["revision"],
                    "origin": origin,
                    "prefix_length": len(prefix),
                    "total_plies": board.move_number - 1,
                    "started_unix": time.time(),
                }
        game.update(
            completed=False,
            reason="worker_error",
            error=repr(error),
            rule_result="unresolved",
            research_result="unresolved",
            finished_unix=time.time(),
        )
        return game

    def commit_game(self, worker, game):
        self.commit_result(worker, game)
        if game["game_id"] not in self.counted_outcomes:
            self.counted_outcomes.add(game["game_id"])
            if game["completed"]:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
        if (
            not game["completed"]
            and (self.consecutive_errors >= 3 or worker.failed)
            and not self.stop_after
        ):
            self.controller.pause_fault(game.get("error", game["reason"]))
        self.refresh_cache()

    def run(self, max_games):
        stored = self.store.config()
        if stored is None:
            self.store.commit(
                "campaign_created",
                {"config": self.config},
                "campaign:" + self.config["campaign_id"],
            )
        elif study_config(stored) != study_config(self.config):
            raise ValueError(
                "Existing campaign config differs; use a new runtime directory"
            )
        elif stored.get("workers", 1) != self.config["workers"]:
            self.store.commit(
                "worker_count_changed",
                {"config": self.config},
                "workers:" + uuid.uuid4().hex,
            )
        self.controller.initialize()
        recovered = self.recover_all()
        self.restore_learning()
        self.running = True
        self.controller.poll(force=True)
        self.controller.finish_stops(recovered)
        self.publish()
        finished = attempts = 0
        retry_after = 0
        executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="makebook-game"
        )
        try:
            while True:
                self.collect_progress()
                self.controls()
                if self.abort:
                    self.abort_signal.set()
                self.refresh_cache()
                changed = False
                commit_failed = False
                for identity, future in list(self.futures.items()):
                    if time.monotonic() < retry_after:
                        commit_failed = self.storage_blocked
                        break
                    if not future.done():
                        continue
                    worker = self.workers[identity - 1]
                    try:
                        game = future.result()
                    except Exception as error:
                        game = self.failed_game(worker, error)
                    try:
                        self.commit_game(worker, game)
                    except (OSError, sqlite3.Error) as error:
                        # Keep the completed future and final spool until commit succeeds.
                        self.io_errors["result_commit"] = str(error)
                        commit_failed = True
                        retry_after = time.monotonic() + 0.25
                        break
                    attempts += 1
                    finished += bool(game["completed"])
                    del self.futures[identity]
                    self.counted_outcomes.discard(game["game_id"])
                    self.activity[identity].update(game_id=None, total_plies=None)
                    worker.current_game = None
                    changed = True
                    print(
                        json.dumps(
                            {
                                "worker_id": identity,
                                "game_id": game["game_id"],
                                "reason": game["reason"],
                                "research_result": game["research_result"],
                                "total_plies": game["total_plies"],
                                "committed_seq": self.store.current_seq(),
                            }
                        ),
                        flush=True,
                    )
                self.storage_blocked = commit_failed
                if not commit_failed:
                    self.io_errors.pop("result_commit", None)
                self.collect_progress()
                if not self.futures and self.stop_after:
                    self.controller.finish_stops()
                setting = self.reconcile_settings()
                desired = setting["values"]["workers"]
                while len(self.workers) < desired:
                    worker = GameWorker(self, len(self.workers) + 1)
                    self.workers.append(worker)
                    self.activity[worker.identity] = {
                        "worker_id": worker.identity,
                        "game_id": None,
                        "total_plies": None,
                    }
                for worker in self.workers:
                    if (
                        worker.identity > desired
                        and worker.identity not in self.futures
                    ):
                        worker.engine.close()
                if changed:
                    self.publish()
                finite_done = max_games is not None and (
                    finished >= max_games or attempts >= max_games + 3
                )
                from .sequential_study import can_dispatch, completed

                finite_done = finite_done or completed(
                    self.config, self.store.service()
                )
                if not self.futures and (self.stop_after or finite_done):
                    break
                if (
                    not self.stop_after
                    and not self.controller.paused
                    and not finite_done
                    and not self.storage_blocked
                ):
                    from .live_settings import target_ready

                    for worker in self.workers[:desired]:
                        if worker.identity in self.futures:
                            continue
                        if not target_ready(
                            self.config, self.store.service(), len(self.futures)
                        ):
                            break
                        if not can_dispatch(
                            self.config, self.store.service(), len(self.futures)
                        ):
                            break
                        if max_games is not None and (
                            finished + len(self.futures) >= max_games
                            or attempts + len(self.futures) >= max_games + 3
                        ):
                            break
                        from .study_plan import default_plan

                        worker.prepare_settings(setting)
                        worker.dispatched_plan = deepcopy(
                            self.store.service().get(
                                "study_plan", default_plan(self.config)
                            )
                        )
                        self.futures[worker.identity] = executor.submit(worker.play)
                        changed = True
                if changed:
                    self.publish()
                time.sleep(0.05)
        finally:
            # Aborted work is saved as unresolved before its checkpoint is removed.
            self.abort_signal.set()
            executor.shutdown(wait=True)
            for identity, future in list(self.futures.items()):
                worker = self.workers[identity - 1]
                try:
                    game = future.result()
                except Exception as error:
                    game = self.failed_game(worker, error)
                try:
                    self.commit_game(worker, game)
                except (OSError, sqlite3.Error, ValueError) as error:
                    self.io_errors["shutdown_commit"] = str(error)
            for worker in self.workers:
                worker.engine.close()
                self.activity[worker.identity].update(game_id=None, total_plies=None)
            self.futures.clear()
            self.current_game = None
            self.running = False
            try:
                self.controller.finish_stops()
                self.publish()
            except (OSError, sqlite3.Error) as error:
                self.io_errors["shutdown_publication"] = str(error)
        return finished, attempts, self.stop_after and not self.failed
