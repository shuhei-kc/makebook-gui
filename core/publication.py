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
        self.tree = {"move": "開始局面", "count": 0, "children": {}}
        self.lines = []
        self.cache = {}
        self.seqs = {}

    def read(self, config, kind):
        # One build per revision, shared by all viewers. This lock is never
        # used by start/status/control or by the independent game manager.
        with self.lock:
            session = config["campaign_id"] if config else None
            if session != self.session:
                self.reset(session)
            db = Path(config["runtime_state_dir"]) / "state.sqlite3" if config else None
            if db is None or not db.exists():
                return Response.make(
                    "#YANEURAOU-DB2016 1.00\n"
                    if kind == "book.db"
                    else ""
                    if kind == "games.sfen"
                    else {"move": "開始局面", "count": 0, "children": {}}
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
                    prefix = config["start_position"].split()
                    prefix_length = (
                        len(prefix) - prefix.index("moves") - 1
                        if "moves" in prefix
                        else 0
                    )
                    rows = conn.execute(
                        """SELECT rowid,
                        json_extract(payload,'$.completed'),
                        json_extract(payload,'$.campaign_id'),
                        json_extract(payload,'$.moves'),
                        json_extract(payload,'$.origin'),
                        json_extract(payload,'$.reason'),
                        json_extract(payload,'$.rule_result')
                        FROM games WHERE rowid>? ORDER BY rowid""",
                        (self.last_row,),
                    )
                    changed = False
                    for row, completed, campaign, moves, origin, reason, result in rows:
                        if completed:
                            if campaign != session:
                                raise ValueError("Foreign game in publication")
                            moves = json.loads(moves)
                            self.lines.append(
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
                            node = self.tree
                            node["count"] += 1
                            for u in moves[prefix_length:]:
                                node = node["children"].setdefault(
                                    u, {"move": u, "count": 0, "children": {}}
                                )
                                node["count"] += 1
                            changed = True
                        self.last_row = row
                    if changed:
                        self.cache.pop("tree", None)
                        self.cache.pop("games.sfen", None)
                    if kind not in self.cache:
                        if kind == "games.sfen":
                            value = "".join(self.lines)
                        else:
                            flat, stack = [], [(self.tree, -1)]
                            while stack:
                                node, parent = stack.pop()
                                identity = len(flat)
                                flat.append(
                                    dict(
                                        id=identity,
                                        parent=parent,
                                        move=node["move"],
                                        count=node["count"],
                                    )
                                )
                                stack.extend(
                                    (child, identity)
                                    for child in reversed(
                                        list(node["children"].values())
                                    )
                                )
                            value = {"count": self.tree["count"], "nodes": flat}
                        self.cache[kind] = Response.make(value)
                self.seqs[kind] = seq
                return self.cache[kind]
            except BaseException:
                self.reset(session)
                raise
            finally:
                conn.close()
