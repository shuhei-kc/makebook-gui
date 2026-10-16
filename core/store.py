"""Single-manager SQLite commits and hash-chained, replayable recovery events."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if (
                    os.name != "nt"
                    or getattr(error, "winerror", None) not in (5, 32, 33)
                    or attempt == 5
                ):
                    raise
                time.sleep(0.01 * 2**attempt)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # A temporary-file cleanup failure never invalidates a commit.


def remove_published(path):
    """Readers may briefly deny deletion on Windows, just as replacement."""
    path = Path(path)
    for attempt in range(6):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in (5, 32, 33)
                or attempt == 5
            ):
                raise
            time.sleep(0.01 * 2**attempt)


class ManagerLock:
    """OS releases the exclusive lock even when the manager is terminated."""

    def __init__(self, directory):
        self.path = Path(directory) / "manager.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Another manager is already running")
        return self

    def __exit__(self, *unused):
        self.handle.close()


class Store:
    def __init__(self, path):
        path = Path(path)
        if str(path).startswith(("\\\\", "//")) or (
            os.name == "nt"
            and ctypes.windll.kernel32.GetDriveTypeW(str(path.resolve().anchor)) == 4
        ):
            raise ValueError("Runtime SQLite must use a local path")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            self.db.close()
            raise ValueError("Primary SQLite integrity check failed")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.export_state = {}
        self.backed_up_seq = None
        self.last_backup = 0
        self.status_cache = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS game_edges (
                game_id TEXT NOT NULL, candidate_id TEXT NOT NULL, generation INTEGER NOT NULL,
                selected_seq INTEGER NOT NULL, verified INTEGER NOT NULL,
                PRIMARY KEY(game_id,candidate_id,generation,selected_seq));
            CREATE TABLE IF NOT EXISTS game_stats (completed INTEGER, result TEXT, count INTEGER NOT NULL,
                PRIMARY KEY(completed,result));
            CREATE INDEX IF NOT EXISTS candidates_active ON candidates(json_extract(payload,'$.active'));
            CREATE INDEX IF NOT EXISTS game_edges_candidate ON game_edges(candidate_id,selected_seq,generation);
            CREATE INDEX IF NOT EXISTS pending_control ON tasks(json_extract(payload,'$.kind'),json_extract(payload,'$.state'));
        """)
        if (
            self.db.execute(
                "SELECT 1 FROM metadata WHERE key='edge_index_version'"
            ).fetchone()
            is None
        ):
            with self.transaction():
                for row in self.db.execute("SELECT payload FROM games"):
                    self.index_game(json.loads(row[0]))
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('edge_index_version','1')"
                )

        if (
            self.db.execute(
                "SELECT 1 FROM metadata WHERE key='game_stats_version'"
            ).fetchone()
            is None
        ):
            with self.transaction():
                self.db.execute("DELETE FROM game_stats")
                self.db.execute(
                    "INSERT INTO game_stats SELECT COALESCE(json_extract(payload,'$.completed'),0),COALESCE(json_extract(payload,'$.research_result'),'unresolved'),COUNT(*) FROM games GROUP BY 1,2"
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('game_stats_version','1')"
                )

    def game_exists(self, identity):
        return (
            self.db.execute("SELECT 1 FROM games WHERE id=?", (identity,)).fetchone()
            is not None
        )

    def index_game(self, game):
        from .learning import candidate_id

        verified = int(
            bool(game.get("completed")) and game.get("research_result") != "unresolved"
        )
        records = {
            (
                game["game_id"],
                candidate_id(item["key"], item["move"]),
                -1 if item.get("generation") is None else item["generation"],
                item.get("selected_seq", 0),
                verified,
            )
            for item in game.get("selections", [])
        }
        self.db.executemany(
            "INSERT OR IGNORE INTO game_edges VALUES (?,?,?,?,?)", records
        )

    def pending_controls(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM tasks WHERE json_extract(payload,'$.kind')='control' "
                "AND json_extract(payload,'$.state') IN ('accepted','waiting_for_game') ORDER BY rowid"
            )
        ]

    def task_progress(self, identity, seq, generation):
        row = self.db.execute(
            "SELECT COUNT(DISTINCT game_id), COUNT(DISTINCT CASE WHEN verified=1 THEN game_id END) "
            "FROM game_edges WHERE candidate_id=? AND selected_seq>=? AND generation>=?",
            (identity, seq, generation),
        ).fetchone()
        return row[0], row[1]

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def candidates(self):
        return {
            row["id"]: json.loads(row["payload"])
            for row in self.db.execute("SELECT * FROM candidates")
        }

    def apply(self, payload):
        for identity, candidate in payload.get("upserts", {}).items():
            self.db.execute(
                "INSERT OR REPLACE INTO candidates VALUES (?,?)",
                (identity, canonical(candidate)),
            )
        if "game" in payload:
            game = payload["game"]
            self.db.execute(
                "INSERT INTO games VALUES (?,?)", (game["game_id"], canonical(game))
            )
            self.index_game(game)
            self.db.execute(
                "INSERT INTO game_stats VALUES (?,?,1) ON CONFLICT(completed,result) DO UPDATE SET count=count+1",
                (
                    int(bool(game.get("completed"))),
                    game.get("research_result", "unresolved"),
                ),
            )
        for task in payload.get("tasks", []) + (
            [payload["task"]] if "task" in payload else []
        ):
            self.db.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?)",
                (task["task_id"], task["request_hash"], canonical(task)),
            )
        if "service" in payload:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('service',?)",
                (canonical(payload["service"]),),
            )
        if "config" in payload:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('config',?)",
                (canonical(payload["config"]),),
            )

    def tasks(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM tasks ORDER BY rowid")
        ]

    def task(self, identity):
        row = self.db.execute(
            "SELECT payload FROM tasks WHERE id=?", (identity,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def service(self):
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key='service'"
        ).fetchone()
        return json.loads(row[0]) if row else {"paused": False, "fault": None}

    def current_seq(self):
        return self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]

    def config(self):
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key='config'"
        ).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, kind, payload, event_id):
        with self.transaction():
            if (
                "game" in payload
                and self.db.execute(
                    "SELECT 1 FROM games WHERE id=?", (payload["game"]["game_id"],)
                ).fetchone()
            ):
                return False
            if kind == "task_accepted":
                existing = self.task(payload["task"]["task_id"])
                if existing:
                    if existing["request_hash"] != payload["task"]["request_hash"]:
                        raise ValueError(
                            "Task ID already exists with different content"
                        )
                    return False
            prior = self.db.execute(
                "SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            service = payload.get("service", self.service())
            version = (
                "backtrack_v4-original-1"
                if service.get("learning_policy") == "legacy_backtrack"
                else "backtrack_v4-generation-1"
            )
            event = {
                "schema": 1,
                "learner_version": payload.get("game", {}).get(
                    "learner_version", version
                ),
                "seq": prior["seq"] + 1 if prior else 1,
                "event_id": event_id,
                "kind": kind,
                "payload": payload,
                "previous_hash": prior["hash"] if prior else "0" * 64,
            }
            if kind == "task_accepted":
                payload["task"]["accepted_seq"] = event["seq"]
            event["hash"] = hashlib.sha256(canonical(event).encode()).hexdigest()
            self.apply(payload)
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)",
                (event["seq"], event["hash"], canonical(event)),
            )
        return True

    def export(self, path, table):
        """Append only new committed rows; recover a torn export tail from SQLite."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached = self.export_state.get(str(path))
        size = path.stat().st_size if path.exists() else 0
        if cached is None or cached[0] != size:
            last = 0
            with path.open("a+b") as stream:
                stream.seek(0, 2)
                end = stream.tell()
                if end:
                    # Find the last complete line without a maximum record size.
                    tail = b""
                    position = end
                    while position > 0 and tail.count(b"\n") < 2:
                        take = min(position, 65536)
                        position -= take
                        stream.seek(position)
                        tail = stream.read(take) + tail
                    complete = tail.rfind(b"\n")
                    if complete < 0:
                        stream.seek(0)
                        stream.truncate()
                        size = 0
                    else:
                        size = end - len(tail) + complete + 1
                        stream.truncate(size)
                        previous = tail.rfind(b"\n", 0, complete)
                        try:
                            record = json.loads(tail[previous + 1 : complete])
                            if table == "events":
                                row = self.db.execute(
                                    "SELECT hash FROM events WHERE seq=?",
                                    (record["seq"],),
                                ).fetchone()
                                if not row or row[0] != record.get("hash"):
                                    raise ValueError(
                                        "Recovery export differs from SQLite"
                                    )
                                last = record["seq"]
                            else:
                                row = self.db.execute(
                                    "SELECT rowid,payload FROM games WHERE id=?",
                                    (record["game_id"],),
                                ).fetchone()
                                if not row or canonical(
                                    json.loads(row[1])
                                ) != canonical(record):
                                    raise ValueError("Game export differs from SQLite")
                                last = row[0]
                        except (ValueError, KeyError, TypeError):
                            # Preserve the damaged disposable export for diagnosis.
                            last = None
            if last is None:
                os.replace(
                    path, path.with_name(path.name + ".damaged-" + uuid.uuid4().hex)
                )
                size, last = 0, 0
            cached = (size, last)
        offset, last = cached
        column = "seq" if table == "events" else "rowid"
        # Keep both memory and the SQLite read cursor bounded per batch.
        with path.open("ab") as stream:
            for identity, payload in self.db.execute(
                f"SELECT {column},payload FROM {table} WHERE {column}>? ORDER BY {column}",
                (last,),
            ):
                stream.write(payload.encode() + b"\n")
                last = identity
            stream.flush()
            os.fsync(stream.fileno())
            offset = stream.tell()
        self.export_state[str(path)] = (offset, last)
        return last

    def replicate(self, path):
        return self.export(path, "events")

    def status(self):
        seq = self.current_seq()
        if self.status_cache and self.status_cache[0] == seq:
            return dict(self.status_cache[1])
        results = {}
        completed = attempts = 0
        for row in self.db.execute("SELECT completed,result,count FROM game_stats"):
            done, result, count = row
            attempts += count
            if done:
                completed += count
                results[result] = results.get(result, 0) + count
        active = self.db.execute(
            "SELECT COUNT(*) FROM candidates WHERE json_extract(payload,'$.active')=1"
        ).fetchone()[0]
        status = {
            "committed_seq": seq,
            "completed_games": completed,
            "attempts": attempts,
            "results": results,
            "active_candidates": active,
        }
        self.status_cache = (seq, status)
        return dict(status)

    def backup(self, directory, force=False):
        seq = self.current_seq()
        if not force and (
            time.monotonic() - self.last_backup < 900 or self.backed_up_seq == seq
        ):
            return self.backed_up_seq
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ("snapshot-" + uuid.uuid4().hex + ".tmp")
        destination = directory / f"state-{seq:012d}-{time.time_ns()}.sqlite3"
        try:
            target = sqlite3.connect(temporary)
            try:
                self.db.backup(target, pages=256)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite backup integrity check failed")
            finally:
                target.close()
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self.backed_up_seq = seq
            self.last_backup = time.monotonic()
            for old in sorted(directory.glob("state-*.sqlite3"))[:-3]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return seq
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def restore(self, path):
        if self.db.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            raise ValueError("Restore requires an empty database")

        def events(stream):
            previous, expected = "0" * 64, 1
            for line in stream:
                if not line.endswith("\n"):
                    raise ValueError("Incomplete recovery log tail")
                event = json.loads(line)
                claimed = event.pop("hash")
                if (
                    event["schema"] != 1
                    or event["seq"] != expected
                    or event["previous_hash"] != previous
                    or hashlib.sha256(canonical(event).encode()).hexdigest() != claimed
                ):
                    raise ValueError(f"Invalid recovery event at seq {expected}")
                event["hash"] = claimed
                previous, expected = claimed, expected + 1
                yield event

        with Path(path).open(encoding="utf-8") as stream:
            count = sum(1 for _ in events(stream))
            stream.seek(0)
            with self.transaction():
                for event in events(stream):
                    self.apply(event["payload"])
                    self.db.execute(
                        "INSERT INTO events VALUES (?,?,?)",
                        (event["seq"], event["hash"], canonical(event)),
                    )
        return count
