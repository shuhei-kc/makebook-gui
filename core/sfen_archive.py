"""Incremental completed SFEN archive and a separate small live projection.

SQLite and the spool are recovery inputs. A committed byte length and SHA
allow an interrupted append to be rolled back. Only the manager writes here.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import time

from .store import atomic_write


def game_line(game):
    origin = game.get("origin", "startpos")
    moves = game.get("moves", [])
    if (
        "\n" in origin
        or "\r" in origin
        or any(not isinstance(m, str) or any(c.isspace() for c in m) for m in moves)
    ):
        raise ValueError("Invalid SFEN export record")
    return origin + (" moves " + " ".join(moves) if moves else "")


def completed_line(game):
    from .exports import terminal

    return (
        game_line(game) + ("" if game.get("moves") else " moves") + " " + terminal(game)
    )


def game_rows(connection, after=0):
    """Read export fields, never selections/info/PV from large payloads."""
    names = (
        "game_id",
        "campaign_id",
        "origin",
        "moves",
        "completed",
        "focus",
        "prefix_length",
        "reason",
        "rule_result",
        "research_result",
        "plan_revision",
        "settings_revision",
        "settings",
    )
    fields = ",".join(f"'{name}',json_extract(payload,'$.{name}')" for name in names)
    return connection.execute(
        f"SELECT rowid,json_object({fields}) FROM games WHERE rowid>? ORDER BY rowid",
        (after,),
    )


class SfenArchive:
    def __init__(self, config):
        self.config = config
        self.path = Path(config["sfen_archive"]) if config.get("sfen_archive") else None
        self.index = None
        self.digest = None
        self.signature = None
        self.needs_index = False
        self.info = {}

    def restore(self, store):
        index_path = self.path.with_suffix(".index.json")
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if (
                index["schema_version"] != 2
                or index["campaign_id"] != self.config["campaign_id"]
            ):
                raise ValueError("Old or foreign archive")
            cursor = index["last_row"]
            last = store.db.execute(
                "SELECT id FROM games WHERE rowid=?", (cursor,)
            ).fetchone()
            if cursor and (last is None or last[0] != index["last_id"]):
                raise ValueError("Archive is ahead of the database")
            digest = sha256()
            remaining = index["sfen_bytes"]
            if remaining < 0:
                raise ValueError("Invalid archive length")
            with self.path.open("r+b") as handle:
                while remaining:
                    block = handle.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError("Incomplete archive")
                    digest.update(block)
                    remaining -= len(block)
                if digest.hexdigest() != index["sfen_sha256"]:
                    raise ValueError("Changed archive")
                handle.truncate(index["sfen_bytes"])
        except (OSError, ValueError, KeyError, TypeError):
            atomic_write(self.path, "")
            digest = sha256()
            index = dict(
                schema_version=2,
                campaign_id=self.config["campaign_id"],
                last_row=0,
                last_id=None,
                sfen_bytes=0,
                sfen_sha256=digest.hexdigest(),
                games=[],
            )
            self.needs_index = True
        self.index, self.digest = index, digest

    def publish(self, store):
        if self.path is None:
            return {}
        index_path = self.path.with_suffix(".index.json")
        if self.index is None:
            self.restore(store)
        index = self.index
        last = store.db.execute("SELECT COALESCE(MAX(rowid),0) FROM games").fetchone()[
            0
        ]
        if (
            last < index["last_row"]
            or not self.path.exists()
            or self.path.stat().st_size != index["sfen_bytes"]
        ):
            self.restore(store)
            index = self.index
        if last != index["last_row"] or self.needs_index or not index_path.exists():
            digest = self.digest.copy()
            entries = list(index["games"])
            cursor, last_id = index["last_row"], index["last_id"]
            try:
                with self.path.open("r+b") as handle:
                    handle.seek(index["sfen_bytes"])
                    handle.truncate()
                    for row, payload in game_rows(store.db, cursor):
                        game = json.loads(payload)
                        cursor, last_id = row, game["game_id"]
                        if not game["completed"]:
                            continue
                        if game["campaign_id"] != self.config["campaign_id"]:
                            raise ValueError("SFEN game belongs to another campaign")
                        content = (completed_line(game) + "\n").encode("utf-8")
                        handle.write(content)
                        digest.update(content)
                        entry = {
                            k: v
                            for k, v in game.items()
                            if k not in ("origin", "moves")
                        }
                        entry.update(line=len(entries) + 1, playing=False)
                        entries.append(entry)
                    handle.flush()
                    os.fsync(handle.fileno())
                    size = handle.tell()
                updated = dict(
                    index,
                    last_row=cursor,
                    last_id=last_id,
                    games=entries,
                    sfen_bytes=size,
                    sfen_sha256=digest.hexdigest(),
                    updated_unix=time.time(),
                )
                atomic_write(index_path, json.dumps(updated, ensure_ascii=False) + "\n")
            except BaseException:
                self.index = None
                raise
            self.index, self.digest = updated, digest
            self.needs_index = False
            index = updated

        runtime = Path(self.config["runtime_state_dir"])
        live = []
        for path in [
            runtime / "in_progress.json",
            *sorted((runtime / "workers").glob("*/in_progress.json")),
        ]:
            try:
                game = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if game.get("campaign_id") != self.config["campaign_id"]:
                raise ValueError("SFEN checkpoint belongs to another campaign")
            if not store.game_exists(game["game_id"]):
                live.append(game)
        signature = tuple((g["game_id"], tuple(g["moves"])) for g in live)
        live_path = self.path.with_suffix(".live.sfen")
        if signature != self.signature or not live_path.exists():
            atomic_write(live_path, "".join(game_line(g) + "\n" for g in live))
            atomic_write(
                self.path.with_suffix(".live.index.json"),
                json.dumps(
                    {
                        "campaign_id": self.config["campaign_id"],
                        "updated_unix": time.time(),
                        "games": [
                            {
                                "line": i,
                                "game_id": g["game_id"],
                                "playing": True,
                                "start_ply": g.get("prefix_length"),
                            }
                            for i, g in enumerate(live, 1)
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n",
            )
            self.signature = signature
        self.info = dict(
            path=str(self.path),
            index_path=str(index_path),
            live_path=str(live_path),
            games=len(index["games"]),
            playing=len(live),
            updated_unix=time.time(),
        )
        return dict(self.info)
