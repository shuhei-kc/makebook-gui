"""Independent single-worker manager with a durable intervention mailbox."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import time
import uuid

import cshogi

from .durable_games import DurableGames, validate_record
from .learning import Candidates, key_of, candidate_id
from .store import ManagerLock, Store, atomic_write, canonical
from .usi import Engine, EngineAborted, EngineError
from .tasks import Controller, study_config, valid_id


def position_board(position):
    words = position.split()
    if words and words[0] == "position":
        words = words[1:]
    if not words:
        raise ValueError("Empty start_position")
    if words[0] == "startpos":
        board, offset, origin = cshogi.Board(), 1, "startpos"
    elif words[0] == "sfen" and len(words) >= 5:
        board, offset = cshogi.Board(sfen=" ".join(words[1:5])), 5
        origin = " ".join(words[:5])
    else:
        raise ValueError("Expected position startpos or position sfen")
    if not board.is_ok():
        raise ValueError("Invalid start board")
    moves = []
    if len(words) > offset:
        if words[offset] != "moves":
            raise ValueError("Expected moves after start board")
        moves = words[offset + 1 :]
    keys, checks = [key_of(board)], []
    for move in moves:
        side = "b" if board.turn == cshogi.BLACK else "w"
        parsed = board.move_from_usi(move)
        if not board.is_legal(parsed):
            raise ValueError(f"Illegal start move {move}")
        board.push(parsed)
        checks.append((side, board.is_check()))
        keys.append(key_of(board))
    return board, origin, moves, keys, checks


def repetition(keys, checks, limit=4):
    if limit not in (2, 4):
        raise ValueError("Repetition limit must be 2 or 4")
    hits = [index for index, key in enumerate(keys) if key == keys[-1]]
    if len(hits) < limit:
        return None
    interval = checks[hits[-limit] :]
    checking = []
    for side in ("b", "w"):
        own_checks = [check for turn, check in interval if turn == side]
        if own_checks and all(own_checks):
            checking.append(side)
    if len(checking) == 1:
        return "perpetual_check", ("white_win" if checking[0] == "b" else "black_win")
    if len(checking) > 1:
        raise EngineError("Ambiguous perpetual-check history")
    return "repetition", "draw"


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("single_worker_version") != 1:
        raise ValueError(
            "Use a single_worker_version=1 campaign; v0.2 examples are proposals"
        )
    workers = config.setdefault("workers", 1)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 to 8")
    board, *_ = position_board(config["start_position"])
    limit = config.get("max_total_plies", 256)
    if not isinstance(limit, int) or limit <= board.move_number - 1:
        raise ValueError("max_total_plies must exceed the starting ply")
    config["max_total_plies"] = limit
    if config.get("entering_king", "27_point") != "27_point":
        raise ValueError("Only the verified 27-point declaration rule is supported")
    config["entering_king"] = "27_point"
    engine = config["engine"]
    engine["executable"] = str((path.parent / engine["executable"]).resolve())
    engine["cwd"] = str(
        (
            path.parent / engine.get("cwd", str(Path(engine["executable"]).parent))
        ).resolve()
    )
    if not Path(engine["executable"]).is_file():
        raise ValueError("Engine executable does not exist")
    if (
        isinstance(engine.get("nodes"), bool)
        or not isinstance(engine.get("nodes"), int)
        or engine["nodes"] <= 0
    ):
        raise ValueError("Engine nodes must be a positive integer")
    for name, default in (
        ("init_timeout_seconds", 120),
        ("ready_timeout_seconds", 600),
        ("search_timeout_seconds", 600),
        ("stop_grace_seconds", 2),
    ):
        value = engine.setdefault(name, default)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 < value <= 3600
        ):
            raise ValueError(f"Invalid {name}")
    config["runtime_state_dir"] = str(
        (path.parent / config["runtime_state_dir"]).resolve()
    )
    runtime = Path(config["runtime_state_dir"])
    if str(runtime).startswith(("\\\\", "//")) or (
        os.name == "nt"
        and ctypes.windll.kernel32.GetDriveTypeW(str(runtime.anchor)) == 4
    ):
        raise ValueError("Runtime SQLite cannot use a network drive")
    outcomes = config.setdefault("research_outcomes", {})
    for reason in ("repetition", "max_total_plies"):
        value = outcomes.setdefault(
            reason, "draw" if reason == "repetition" else "unresolved"
        )
        allowed = {"black_win", "white_win", "draw"} | (
            {"unresolved"} if reason == "max_total_plies" else set()
        )
        if value not in allowed:
            raise ValueError(f"Invalid research outcome for {reason}")
    valid_id(config["campaign_id"])
    for field, default in (
        ("queue_dir", str(runtime / "tasks")),
        ("result_dir", str(runtime / "results")),
    ):
        config[field] = str((path.parent / config.get(field, default)).resolve())
    for field, default in (("poll_seconds", 0.25), ("status_seconds", 1)):
        value = config.setdefault(field, default)
        if type(value) not in (int, float) or not 0.1 <= value <= 60:
            raise ValueError(f"Invalid {field}")
    if "start_paused" in config and type(config["start_paused"]) is not bool:
        raise ValueError("start_paused must be boolean")
    config.setdefault("seed", None)  # Legacy config identity only; never seeds the RNG.
    config.setdefault("fixed_rules", [])
    for line in config.get("reference_variations", []):
        reference, *_ = position_board(config["start_position"])
        for move in line["moves"]:
            parsed = reference.move_from_usi(move)
            if not reference.is_legal(parsed):
                raise ValueError("Illegal reference variation")
            reference.push(parsed)
    for rule in config["fixed_rules"]:
        test_board = cshogi.Board(sfen=rule["canonical_sfen"] + " 1")
        if not test_board.is_ok() or not rule["moves"]:
            raise ValueError("Invalid fixed rule")
        for item in rule["moves"]:
            if (
                not test_board.is_legal(test_board.move_from_usi(item["usi"]))
                or item.get("weight", 1) <= 0
            ):
                raise ValueError("Illegal fixed move or nonpositive weight")
    from .sequential_study import validate

    validate(config)
    if config.get("sfen_archive"):
        config["sfen_archive"] = str((path.parent / config["sfen_archive"]).resolve())
        if Path(config["sfen_archive"]).suffix.lower() != ".sfen":
            raise ValueError("SFEN archive requires a .sfen file")
    return config


class Runner:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.runtime = Path(config["runtime_state_dir"])
        self.control = self.runtime / "control"
        self.control.mkdir(parents=True, exist_ok=True)
        self.book = Candidates(store.candidates())
        self.random = random
        self.stop_after = False
        self.abort = False
        self.published_seq = 0
        self.publication_error = None
        self.failed = False
        self.current_game = None
        self.current_total_plies = None
        self.running = False
        self.last_heartbeat = 0
        self.consecutive_errors = 0
        self.durable = DurableGames(self.runtime)
        self.last_game = None
        self.dispatched_plan = None
        self.settings_revision = 0
        self.game_settings = None
        self.io_errors = {}
        self.storage_blocked = False
        self.last_export = 0
        from .sfen_archive import SfenArchive

        self.sfen_archive = SfenArchive(config)
        options = dict(config["engine"].get("usi_options", {}))
        profile = config["engine"]
        self.engine = Engine(
            [profile["executable"], *profile.get("arguments", [])],
            cwd=profile["cwd"],
            options=options,
            log_path=self.runtime / "usi.jsonl",
            init_timeout=profile["init_timeout_seconds"],
            ready_timeout=profile["ready_timeout_seconds"],
            stop_grace=profile["stop_grace_seconds"],
            log_search_details=config.get("save_search_details", False),
        )
        self.controller = Controller(config, store, self)

    def checkpoint(self, game, moves, board):
        self.current_total_plies = board.move_number - 1
        partial = game | {"moves": list(moves), "total_plies": board.move_number - 1}
        # Required recovery input is private; GUI is only a disposable projection.
        self.durable.checkpoint(partial)
        try:
            atomic_write(self.runtime / "in_progress.json", canonical(partial) + "\n")
            self.io_errors.pop("live_projection", None)
        except OSError as error:
            self.io_errors["live_projection"] = str(error)

    def prepare_settings(self, setting):
        """Called only while this slot has no outstanding game."""
        from .live_settings import effective_config

        config = effective_config(self.config, setting["values"])
        old = self.config["engine"]
        new = config["engine"]
        options = dict(new.get("usi_options", {}))
        if old["executable"] != new["executable"] or old["cwd"] != new["cwd"]:
            self.engine.close()
            self.engine.command = [new["executable"]]
            self.engine.cwd = new["cwd"]
        if self.engine.options != options:
            self.engine.close()
            self.engine.options = options
        self.engine.init_timeout = new["init_timeout_seconds"]
        self.engine.ready_timeout = new["ready_timeout_seconds"]
        self.engine.stop_grace = new["stop_grace_seconds"]
        self.config = config
        self.settings_revision = setting["revision"]
        from copy import deepcopy

        self.game_settings = deepcopy(setting["values"])

    def reconcile_settings(self):
        from .live_settings import apply_target, current

        service = self.store.service()
        outstanding = (
            len(self.futures)
            if hasattr(self, "futures")
            else int(bool(self.current_game))
        )
        changed = apply_target(self.controller.config, service, outstanding)
        if changed:
            self.store.commit(
                "settings_target_applied",
                {"service": changed},
                "settings-target:" + uuid.uuid4().hex,
            )
            service = changed
        setting = current(self.controller.config, service)
        identity = setting.get("task_id")
        task = self.store.task(identity) if identity else None
        if task and task["state"] == "waiting_for_settings":
            if hasattr(self, "futures"):
                waiting = [
                    i
                    for i in self.futures
                    if self.workers[i - 1].settings_revision != setting["revision"]
                ]
            else:
                waiting = (
                    [1]
                    if self.current_game
                    and self.settings_revision != setting["revision"]
                    else []
                )
            target_pending = bool(
                service.get("sequential_study")
                and not service["sequential_study"]["done"]
                and setting["values"]["target_black_streak"]
                != service["sequential_study"]["target_black_streak"]
            )
            if not waiting and not target_pending:
                task.update(
                    state="completed", applied_unix=time.time(), response=setting
                )
                self.store.commit(
                    "settings_applied", {"task": task}, "settings-applied:" + identity
                )
                self.controller.publish()
        return setting

    def cleanup_projection(self, worker, identity):
        path = worker.runtime / "in_progress.json"
        try:
            if (
                path.exists()
                and json.loads(path.read_text(encoding="utf-8")).get("game_id")
                == identity
            ):
                path.unlink(missing_ok=True)
            worker.io_errors.pop("checkpoint_cleanup", None)
        except (OSError, ValueError) as error:
            worker.io_errors["checkpoint_cleanup"] = str(error)

    def commit_result(self, worker, game):
        # No in-memory learning mutation until the atomic SQLite commit succeeds.
        if not self.store.game_exists(game["game_id"]):
            policy = self.store.service().get("learning_policy", "legacy_backtrack")
            game["learner_version"] = (
                "makebook2-compatible-1"
                if policy == "legacy_backtrack"
                else "unsupported-generation-policy"
            )
            validate_record(game, self.config)
            worker.durable.result(game)
            # A bounded working set contains every alternative at visited positions.
            keys = {item["key"] for item in game["selections"]}
            updated = Candidates(
                {row["id"]: row for key in keys for row in self.book.rows(key)}
            )
            changes = {}
            if game["completed"] and game["research_result"] != "unresolved":
                winner = {"black_win": "b", "white_win": "w", "draw": None}[
                    game["research_result"]
                ]
                changes = updated.learn(
                    game["selections"], winner, game["game_id"], policy=policy
                )
            payload = {"game": game, "upserts": changes}
            from .sequential_study import record_game

            service = record_game(self.config, self.store.service(), game)
            if service is not None:
                payload["service"] = service
            self.store.commit("game_finished", payload, "game:" + game["game_id"])
            self.book.apply(changes)
            self.cache_delta = changes
        else:
            # COMMIT may have succeeded before the caller observed an I/O error.
            # Reconcile the bounded affected set without applying learning again.
            changes = {}
            for identity in {
                candidate_id(item["key"], item["move"]) for item in game["selections"]
            }:
                row = self.store.db.execute(
                    "SELECT payload FROM candidates WHERE id=?", (identity,)
                ).fetchone()
                if row:
                    changes[identity] = json.loads(row[0])
            self.book.apply(changes)
            self.cache_delta = changes
        worker.durable.retire(game["game_id"])
        self.cleanup_projection(worker, game["game_id"])

    def recover_runtime(self, worker):
        recovered = None
        for game, terminal in worker.durable.records(self.store.game_exists):
            validate_record(game, self.config)
            if not terminal:
                game.update(
                    completed=False,
                    reason="manager_restart",
                    rule_result="unresolved",
                    research_result="unresolved",
                    finished_unix=time.time(),
                )
            self.commit_result(worker, game)
            recovered = game["game_id"]
        # Backwards-compatible migration of pre-spool unfinished checkpoints.
        path = worker.runtime / "in_progress.json"
        if path.exists():
            game = json.loads(path.read_text(encoding="utf-8"))
            if game["campaign_id"] != self.config["campaign_id"]:
                raise ValueError("Interrupted game belongs to another campaign")
            if not self.store.game_exists(game["game_id"]):
                game.update(
                    completed=False,
                    reason="manager_restart",
                    rule_result="unresolved",
                    research_result="unresolved",
                    finished_unix=time.time(),
                )
                self.store.commit(
                    "game_finished",
                    {"game": game, "upserts": {}},
                    "game:" + game["game_id"],
                )
                self.book = Candidates(self.store.candidates())
                recovered = game["game_id"]
            self.cleanup_projection(worker, game["game_id"])
        return recovered

    def recover_interrupted(self):
        return self.recover_runtime(self)

    def restore_learning(self):
        from .legacy_restore import apply_requested_restore

        result = apply_requested_restore(self.config, self.store)
        if result is not None:
            self.book = Candidates(self.store.candidates())
        return result

    def controls(self):
        self.controller.poll()
        for name in ("stop_after_games", "abort_now", "dump"):
            marker = self.control / name
            try:
                marker.unlink()
            except FileNotFoundError:
                continue
            if name == "stop_after_games":
                self.stop_after = True
            elif name == "abort_now":
                self.abort = True
                self.stop_after = True
            else:
                self.publish()
        if time.monotonic() - self.last_heartbeat >= self.config.get(
            "status_seconds", 1
        ):
            self.publish(full=False)
        self.durable.cleanup()
        return self.abort

    def status_extra(self):
        return {
            "workers_count": 1,
            "active_workers": int(bool(self.current_game)),
            "io_errors": dict(self.io_errors),
            "storage_blocked": self.storage_blocked,
            "cleanup_pending": len(self.durable.cleanup_pending),
            "protocol_log_errors": {"1": self.engine.log_error}
            if self.engine.log_error
            else {},
        }

    def publish(self, full=True):
        full = full or time.monotonic() - self.last_export >= 30
        if full:
            self.last_export = time.monotonic()
            try:
                self.published_seq = self.store.replicate(
                    self.runtime / "recovery" / "events.jsonl"
                )
                self.publication_error = None
            except (OSError, ValueError) as error:
                self.publication_error = str(error)
        if full:
            try:
                self.store.backup(
                    self.runtime / "recovery" / "snapshots",
                    force=not self.running and not self.current_game,
                )
                self.io_errors.pop("backup", None)
            except (OSError, sqlite3.Error, ValueError) as error:
                self.io_errors["backup"] = str(error)
        service = self.store.service()
        try:
            archive = self.sfen_archive.publish(self.store)
            self.io_errors.pop("sfen_export", None)
        except (OSError, ValueError, sqlite3.Error) as error:
            self.io_errors["sfen_export"] = str(error)
            archive = self.sfen_archive.info | {"error": str(error)}
        phase = (
            "faulted"
            if self.storage_blocked
            else "playing"
            if self.current_game
            else "faulted"
            if self.running and service.get("fault")
            else "paused"
            if self.running and service["paused"]
            else "running"
            if self.running
            else "stopped"
        )
        status = self.store.status() | {
            "campaign_id": self.config["campaign_id"],
            "manager_pid": os.getpid(),
            "phase": phase,
            "paused": service["paused"],
            "fault": service.get("fault"),
            "queue_dir": str(self.controller.queue),
            "result_dir": str(self.controller.results),
            "task_poll_error": self.controller.poll_error,
            "manager_log_error": getattr(sys.stdout, "error", None),
            "task_result_error": self.controller.output_error,
            "published_seq": self.published_seq,
            "backed_up_seq": self.store.backed_up_seq,
            "publication_error": self.publication_error,
            "current_game_id": self.current_game,
            "current_total_plies": self.current_total_plies
            if self.current_game
            else None,
            "updated_unix": time.time(),
            "stop_after_games": self.stop_after,
            "abort_now": self.abort,
        }
        from .study_plan import default_plan

        status["study_plan"] = service.get("study_plan", default_plan(self.config))
        from .live_settings import current

        status["runtime_settings"] = current(self.controller.config, service)
        status["settings_supported"] = True
        if self.config.get("sequential_study"):
            from .sequential_study import progress

            status["sequential_study"] = progress(self.config, service)
        if self.sfen_archive.path is not None:
            status["sfen_archive"] = archive
        status.update(self.status_extra())
        # A remote reader can retain a Windows handle to status.json for hours.
        # Publish a second atomic projection, and never gate the game journal
        # on either status file being replaceable.
        serialized = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
        published = False
        errors = []
        for name in ("status-live.json", "status.json"):
            try:
                atomic_write(self.runtime / name, serialized)
                published = True
            except (OSError, ValueError) as error:
                errors.append(str(error))
        if published:
            self.io_errors.pop("publication", None)
        else:
            self.io_errors["publication"] = "; ".join(errors)
        try:
            atomic_write(
                self.runtime / "health.json",
                canonical(
                    {
                        key: status.get(key)
                        for key in (
                            "campaign_id",
                            "manager_pid",
                            "updated_unix",
                            "phase",
                            "paused",
                            "fault",
                        )
                    }
                    | {"fatal_exit": False}
                )
                + "\n",
            )
            self.io_errors.pop("health_publication", None)
        except OSError as error:
            self.io_errors["health_publication"] = str(error)
        try:
            if full:
                self.store.export(self.runtime / "games.jsonl", "games")
                self.controller.publish()
        except (OSError, ValueError) as error:
            self.io_errors["game_export"] = str(error)
        else:
            self.io_errors.pop("game_export", None)
        self.last_heartbeat = time.monotonic()
        return status

    def play(self):
        config = self.config
        from .study_plan import default_plan, game_start
        from copy import deepcopy

        plan = deepcopy(
            self.dispatched_plan
            or self.store.service().get("study_plan", default_plan(config))
        )
        self.failed, self.last_game = False, None
        game = {
            "game_id": str(uuid.uuid4()),
            "campaign_id": config["campaign_id"],
            "completed": False,
            "reason": None,
            "rule_result": "unresolved",
            "research_result": "unresolved",
            "selections": [],
            "moves": [],
            "started_unix": time.time(),
        }
        board, origin, prefix, keys, checks = game_start(config, plan)
        repetition_limit = config.get("repetition_limit", 2)
        # Reference moves connect the exported tree; repetition starts here,
        # for both fourfold repetition and the optional twofold study rule.
        keys, checks = [key_of(board)], []
        self.current_game = game["game_id"]
        moves = list(prefix)
        game.update(
            origin=origin,
            prefix_length=len(prefix),
            tree_prefix_length=len(position_board(config["start_position"])[2]),
            focus=plan["focus"],
            plan_revision=plan["revision"],
            fixed_rules=plan["fixed_rules"],
            history_before_start_known=origin == "startpos",
            repetition_history_start_ply=len(prefix),
            engine_position_mode="current_sfen",
            engine_ready_scope="process_start",
            book_random_mode="unseeded_global",
            engine_protocol_version="mkbook-current-sfen-1",
        )
        from .live_settings import baseline

        game.update(
            settings_revision=self.settings_revision,
            settings=deepcopy(self.game_settings or baseline(config)),
        )
        try:
            self.checkpoint(game, moves, board)
            self.publish()
            if self.engine.process is None:
                self.engine.start(self.controls)
            while True:
                if self.controls():
                    raise EngineAborted("Immediate abort requested")
                end = repetition(keys, checks, repetition_limit)
                if end:
                    early_loop = repetition_limit == 2 and end[0] == "repetition"
                    game.update(
                        reason="loop_abort" if early_loop else end[0],
                        rule_result="unresolved" if early_loop else end[1],
                        completed=True,
                    )
                    game["research_result"] = (
                        config["research_outcomes"]["repetition"]
                        if end[0] == "repetition"
                        else end[1]
                    )
                    break
                if board.is_game_over():
                    winner = "w" if board.turn == cshogi.BLACK else "b"
                    result = "black_win" if winner == "b" else "white_win"
                    game.update(
                        reason="mate",
                        rule_result=result,
                        research_result=result,
                        completed=True,
                    )
                    break
                if board.move_number - 1 >= config["max_total_plies"]:
                    game.update(
                        reason="max_total_plies",
                        completed=True,
                        research_result=config["research_outcomes"]["max_total_plies"],
                    )
                    break
                key, info = key_of(board), None
                selected_seq = self.store.current_seq()
                search_generations = None
                search_active = None
                fixed = next(
                    (
                        rule
                        for rule in plan["fixed_rules"]
                        if rule["canonical_sfen"] == key
                    ),
                    None,
                )
                active = self.book.active(key)
                if fixed:
                    choice = self.random.choices(
                        fixed["moves"],
                        [item.get("weight", 1) for item in fixed["moves"]],
                    )[0]
                    move, source = choice["usi"], "fixed"
                elif active:
                    move, source = self.random.choice(active)["move"], "book"
                else:
                    search_generations = {
                        row["move"]: row["generation"] for row in self.book.rows(key)
                    }
                    search_active = {
                        row["move"]: row["active"] for row in self.book.rows(key)
                    }
                    position = "position sfen " + board.sfen()
                    profile = config["engine"]
                    move, info = self.engine.search(
                        position,
                        profile["nodes"],
                        profile["search_timeout_seconds"],
                        self.controls,
                    )
                    if not config.get("save_search_details", False):
                        # Keep only the small display score; PV and search counters
                        # are optional diagnostics, not learning/recovery inputs.
                        info = (
                            {"score": info["score"]}
                            if info and "score" in info
                            else None
                        )
                    source = "engine"
                side = "b" if board.turn == cshogi.BLACK else "w"
                if move in ("resign", "win"):
                    if move == "win" and not board.is_nyugyoku():
                        raise EngineError("Unverified 27-point declaration")
                    winner = side if move == "win" else ("w" if side == "b" else "b")
                    result = "black_win" if winner == "b" else "white_win"
                    game.update(
                        reason="declaration" if move == "win" else "resign",
                        terminal=move,
                        rule_result=result,
                        research_result=result,
                        completed=True,
                        terminal_info=info,
                    )
                    break
                parsed = board.move_from_usi(move)
                if not board.is_legal(parsed):
                    raise EngineError(f"Illegal bestmove: {move}")
                selection = self.book.selection(key, move, source, info)
                selection["selected_seq"] = selected_seq
                if search_generations is not None:
                    selection["generation"] = search_generations.get(move)
                    selection["active_when_selected"] = search_active.get(move, False)
                game["selections"].append(selection)
                board.push(parsed)
                moves.append(move)
                keys.append(key_of(board))
                checks.append((side, board.is_check()))
                self.checkpoint(game, moves, board)
        except KeyboardInterrupt:
            game.update(reason="aborted", error="KeyboardInterrupt")
            self.abort = self.stop_after = True
            self.engine.close()
        except OSError as error:
            game.update(reason="persistence_error", error=str(error))
            self.failed = True
            self.engine.close()
        except EngineAborted as error:
            game.update(reason="aborted", error=str(error))
            self.engine.close()
        except (EngineError, ValueError) as error:
            game.update(reason="engine_error", error=str(error))
            self.engine.close()
        finally:
            game.update(
                moves=moves,
                total_plies=board.move_number - 1,
                finished_unix=time.time(),
            )
            self.last_game = game
        # Preserve completed outcomes before returning them to the coordinator.
        self.durable.result(game)
        return game

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
        self.controller.initialize()
        self.running = True
        recovered = self.recover_interrupted()
        self.restore_learning()
        self.controller.poll(force=True)
        self.controller.finish_stops(recovered)
        self.publish()
        attempts = finished = 0
        try:
            while (
                max_games is None or finished < max_games and attempts < max_games + 3
            ):
                if self.controls() or self.stop_after:
                    break
                from .sequential_study import completed

                if completed(self.config, self.store.service()):
                    break
                if self.controller.paused:
                    time.sleep(self.config.get("poll_seconds", 0.25))
                    continue
                self.prepare_settings(self.reconcile_settings())
                try:
                    game = self.play()
                except OSError as error:
                    if self.last_game is None:
                        raise
                    game = self.last_game
                    self.io_errors["result_spool"] = str(error)
                while True:
                    try:
                        self.commit_result(self, game)
                        self.storage_blocked = False
                        self.io_errors.pop("result_commit", None)
                        break
                    except (OSError, sqlite3.Error) as error:
                        self.storage_blocked = True
                        self.io_errors["result_commit"] = str(error)
                        self.controls()
                        time.sleep(0.25)
                attempts += 1
                if self.failed:
                    self.controller.pause_fault(
                        game.get("error", "Required checkpoint persistence failed")
                    )
                if game["completed"]:
                    finished += 1
                    self.consecutive_errors = 0
                else:
                    self.consecutive_errors += 1
                    if (
                        max_games is None
                        and self.consecutive_errors >= 3
                        and not self.stop_after
                    ):
                        self.controller.pause_fault(game.get("error", game["reason"]))
                self.controller.finish_stops(game["game_id"])
                self.current_game = None
                status = self.publish()
                print(
                    json.dumps(
                        {
                            "game_id": game["game_id"],
                            "reason": game["reason"],
                            "research_result": game["research_result"],
                            "total_plies": game["total_plies"],
                            "committed_seq": status["committed_seq"],
                        }
                    ),
                    flush=True,
                )
                if (
                    not game["completed"]
                    and game["reason"] == "engine_error"
                    and not self.stop_after
                ):
                    time.sleep(0.1)
        finally:
            self.engine.close()
            self.current_game = None
            self.running = False
            try:
                self.controller.finish_stops()
                self.publish()
            except (OSError, sqlite3.Error):
                pass  # Private spool survives a terminal publication failure.
        return finished, attempts, self.stop_after and not self.failed


def run_campaign(config, max_games):
    with ManagerLock(config["runtime_state_dir"]):
        store = Store(Path(config["runtime_state_dir"]) / "state.sqlite3")
        try:
            # A campaign started with one slot can grow without a new manager.
            from .parallel import ParallelRunner

            return ParallelRunner(config, store).run(max_games)
        finally:
            store.close()
