"""Shared, revision-cached spectator projections, separate from control locks."""

from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading

from .exports import book_text
from .sfen_archive import completed_line
from .view_tree import ViewTree


@dataclass(frozen=True)
class Response:
    body: bytes
    compressed: bytes
    etag: str

    @classmethod
    def make(cls, value):
        body = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ).encode("utf-8")
        return cls(
            body,
            gzip.compress(body, compresslevel=1, mtime=0),
            'W/"' + sha256(body).hexdigest() + '"',
        )


class Publications:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset(None)

    def reset(self, session):
        self.session = session
        self.last_row = 0
        self.tree = None
        self.lines = []
        self.cache = {}
        self.seqs = {}
        self.task_trees = {}
        self.task_lines = {}
        self.tasks = []

    @staticmethod
    def focuses(config):
        return (config or {}).get("sequential_study", {}).get("focuses") or [
            {"id": "anchor", "label": "開始局面", "moves": []}
        ]

    def prepare_trees(self, config):
        start_tree = ViewTree(config)
        self.tree = ViewTree(
            config | {"start_position": "position " + start_tree.origin}
        )
        self.tree.walk(start_tree.prefix, reference=True)
        for number, focus in enumerate(self.focuses(config), 1):
            moves = start_tree.prefix + focus["moves"]
            index, _ = self.tree.walk(moves, reference=True)
            start = "position " + self.tree.origin
            if moves:
                start += " moves " + " ".join(moves)
            task_tree = ViewTree(config | {"start_position": start})
            self.task_trees[focus["id"]] = task_tree
            self.task_lines[focus["id"]] = []
            last = moves[-1] if moves else ""
            self.tasks.append(
                dict(
                    id=focus["id"],
                    n=number,
                    label=focus["label"],
                    comment="",
                    sfen=task_tree.nodes[0]["sfen"],
                    ply=task_tree.base,
                    node=self.tree.path(index),
                    move=self.tree.nodes[index]["label"],
                    last=([last[2:4]] if "*" in last else [last[:2], last[2:4]])
                    if last
                    else [],
                )
            )

    def read(self, config, kind):
        # One build per revision, shared by all viewers. This lock is never
        # used by start/status/control or by the independent game manager.
        with self.lock:
            session = config["campaign_id"] if config else None
            if session != self.session:
                self.reset(session)
            task_id = kind.split(":", 1)[1] if ":" in kind else None
            if task_id is not None and task_id not in {
                f["id"] for f in self.focuses(config) if config
            }:
                raise ValueError("課題が見つかりません")
            db = Path(config["runtime_state_dir"]) / "state.sqlite3" if config else None
            if db is None or not db.exists():
                return Response.make(
                    "#YANEURAOU-DB2016 1.00\n"
                    if kind == "book.db"
                    else ""
                    if kind == "games.sfen"
                    else ViewTree({"start_position": "position startpos"}).export()
                )
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.5)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events"
                ).fetchone()[0]
                if seq < max(self.seqs.values(), default=0):
                    self.reset(session)
                if self.seqs.get(kind) == seq:
                    return self.cache[kind]
                if kind == "book.db":
                    rows = [
                        json.loads(row[0])
                        for row in conn.execute("SELECT payload FROM candidates")
                    ]
                    value, _ = book_text(rows)
                    self.cache[kind] = Response.make(value)
                else:
                    # Completed rows are immutable. Transfer only the moves and
                    # ending; large search-info payloads remain inside SQLite.
                    if self.tree is None:
                        self.prepare_trees(config)
                    rows = conn.execute(
                        """SELECT rowid,
                        json_extract(payload,'$.completed'),
                        json_extract(payload,'$.campaign_id'),
                        json_extract(payload,'$.moves'),
                        json_extract(payload,'$.origin'),
                        json_extract(payload,'$.reason'),
                        json_extract(payload,'$.rule_result'),
                        COALESCE(json_extract(payload,'$.worker_id'),1),
                        json_extract(payload,'$.game_id'),
                        json_extract(payload,'$.focus.id'),
                        json_extract(payload,'$.prefix_length')
                        FROM games WHERE rowid>? ORDER BY rowid""",
                        (self.last_row,),
                    )
                    changed = False
                    for (
                        row,
                        completed,
                        campaign,
                        moves,
                        origin,
                        reason,
                        result,
                        worker,
                        identity,
                        focus_id,
                        prefix_length,
                    ) in rows:
                        if completed:
                            if campaign != session:
                                raise ValueError("Foreign game in publication")
                            moves = json.loads(moves)
                            if focus_id is None and not config.get("sequential_study"):
                                focus_id = "anchor"
                            line = (
                                completed_line(
                                    dict(
                                        origin=origin,
                                        moves=moves,
                                        reason=reason,
                                        rule_result=result,
                                    )
                                )
                                + "\n"
                            )
                            self.lines.append(line)
                            self.tree.add(
                                moves,
                                result,
                                identity,
                                worker,
                                origin,
                                reason,
                                focus_id,
                                prefix_length,
                                stop_ply=prefix_length
                                if focus_id in self.task_trees
                                else None,
                            )
                            if focus_id in self.task_trees:
                                self.task_trees[focus_id].add(
                                    moves,
                                    result,
                                    identity,
                                    worker,
                                    origin,
                                    reason,
                                    focus_id,
                                    prefix_length,
                                )
                                self.task_lines[focus_id].append(line)
                            changed = True
                        self.last_row = row
                    if changed:
                        for name in list(self.cache):
                            if name != "book.db":
                                self.cache.pop(name, None)
                                self.seqs.pop(name, None)
                    if kind not in self.cache:
                        if kind == "games.sfen":
                            value = "".join(self.lines)
                        elif kind.startswith("sfen:"):
                            value = "".join(self.task_lines[task_id])
                        elif kind.startswith("task:"):
                            value = self.task_trees[task_id].export() | {
                                "id": task_id,
                                "revision": self.last_row,
                            }
                        else:
                            tasks = [
                                dict(
                                    task,
                                    sfen_games=self.task_trees[task["id"]].completed,
                                    games=self.task_trees[task["id"]].completed,
                                    wins=self.task_trees[task["id"]].nodes[0]["b"],
                                    losses=self.task_trees[task["id"]].nodes[0]["w"],
                                    black_streak=self.task_trees[
                                        task["id"]
                                    ].black_streak,
                                    best_black_streak=self.task_trees[
                                        task["id"]
                                    ].best_black_streak,
                                )
                                for task in self.tasks
                            ]
                            value = self.tree.export() | {
                                "revision": self.last_row,
                                "tasks": tasks,
                                "target_black_streak": config.get(
                                    "sequential_study", {}
                                ).get("target_black_streak"),
                            }
                        self.cache[kind] = Response.make(value)
                self.seqs[kind] = seq
                return self.cache[kind]
            except BaseException:
                self.reset(session)
                raise
            finally:
                conn.close()
