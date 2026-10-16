"""Learning from tayayan/HiraganaSuisho makebook2.py (MIT).
See docs/ALGORITHM.md and docs/upstream for the pinned source.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import cshogi


def key_of(board):
    return " ".join(board.sfen().split()[:3])


def candidate_id(key, move):
    return sha256((key + "\n" + move).encode()).hexdigest()


def read_book(path):
    book, key = {}, None
    # Four-column records omit the adoption count; imports start at one.
    # Do not silently discard undecodable bytes or malformed move records.
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for number, raw in enumerate(handle, 1):
                fields = raw.split()
                if not fields or fields[0].startswith("#"):
                    continue
                if fields[0] == "sfen":
                    if len(fields) not in (4, 5):
                        raise ValueError(f"定跡DB {number}行目: SFENの形式が不正です")
                    key = " ".join(fields[1:4])
                    book.setdefault(key, {})
                else:
                    if key is None or len(fields) < 4:
                        raise ValueError(
                            f"定跡DB {number}行目: 指し手行には4列以上が必要です"
                        )
                    try:
                        count = int(fields[4]) if len(fields) >= 5 else 1
                    except ValueError as error:
                        raise ValueError(
                            f"定跡DB {number}行目: 採用数が整数ではありません"
                        ) from error
                    book[key][fields[0]] = count
    except UnicodeError as error:
        raise ValueError("定跡DBはUTF-8で保存してください") from error
    return {key: moves for key, moves in book.items() if moves}


def legacy_learn(book, selections, loser):
    """Exact historical backtracking, including historical repeated wins."""
    stop = False
    for item in reversed(selections):
        key, move = item["key"], item["move"]
        if key.split()[1] == loser and not stop:
            moves = book.get(key)
            if moves is not None:
                moves.pop(move, None)
                if moves:
                    stop = True
                else:
                    book.pop(key)
        else:
            moves = book.setdefault(key, {})
            moves[move] = moves.get(move, 0) + 1


def replay_legacy(directory):
    directory = Path(directory)
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    base = int(status["base_sfen"].split()[-1]) - 1
    kifs = sorted(directory.glob("kif*.sfen"))
    if len(kifs) != 1:
        raise ValueError("Expected exactly one saved kif stream")
    lines = [
        line.split()[2:]
        for line in kifs[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    outcomes = [
        json.loads(line)
        for line in (directory / "outcomes.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    if len(lines) != len(outcomes):
        raise ValueError("Saved kif/outcome lengths differ")
    book, board = {}, cshogi.Board()
    for move in lines[0][:base]:
        book.setdefault(key_of(board), {})[move] = 1000
        board.push_usi(move)
    for moves, outcome in zip(lines, outcomes):
        board, selections = cshogi.Board(), []
        for index, move in enumerate(moves):
            if move in {"resign", "win", "rep_draw", "max_moves"}:
                break
            parsed = board.move_from_usi(move)
            if not board.is_legal(parsed):
                raise ValueError(f"Illegal fixture move {move}")
            if index >= base:
                selections.append({"key": key_of(board), "move": move})
            board.push(parsed)
        legacy_learn(book, selections, "w" if outcome["winner"] == "b" else "b")
    return book, len(outcomes)


class Candidates:
    def __init__(self, state=None):
        self.state = deepcopy(state or {})
        self.by_key = {}
        for row in self.state.values():
            self.by_key.setdefault(row["key"], {})[row["id"]] = row

    def apply(self, changes):
        for identity, row in changes.items():
            row = deepcopy(row)
            self.state[identity] = row
            self.by_key.setdefault(row["key"], {})[identity] = row

    def rows(self, key):
        return self.by_key.get(key, {}).values()

    def active(self, key):
        return [row for row in self.rows(key) if row["active"]]

    def register(self, key, move, reactivate=False, legacy_count=0):
        identity = candidate_id(key, move)
        row = self.state.get(identity)
        if row is None:
            row = self.state[identity] = {
                "id": identity,
                "key": key,
                "move": move,
                "generation": 1,
                "active": True,
                "wins": 0,
                "lifetime_wins": 0,
                "games": 0,
                "visits": 0,
                "weight": 1.0,
                "legacy_count": legacy_count,
                "reason": "registered",
            }
            self.by_key.setdefault(key, {})[identity] = row
        elif reactivate and not row["active"]:
            row.update(
                generation=row["generation"] + 1,
                active=True,
                wins=0,
                reason="manual_reactivation",
            )
        return row

    def selection(self, key, move, source, info=None):
        row = self.state.get(candidate_id(key, move))
        return {
            "key": key,
            "move": move,
            "source": source,
            "generation": row["generation"] if row else None,
            "info": info,
        }

    def learn(self, selections, winner, game_id, policy="legacy_backtrack"):
        if policy != "legacy_backtrack":
            raise ValueError("The public version uses makebook2 learning")
        return self.learn_legacy(selections, winner, game_id)

    def learn_legacy(self, selections, winner, game_id):
        """Original book decisions: generations never reject a win or a loss.

        Metadata keeps unique-game observations; book counts follow every
        winning occurrence, exactly as legacy_learn. Restoring a deleted move
        starts its book count at one, irrespective of its old saved count.
        """
        ordinary = list(selections)
        touched = {candidate_id(item["key"], item["move"]) for item in ordinary}
        before = {identity: deepcopy(self.state.get(identity)) for identity in touched}
        book = {
            key: {row["move"]: row["wins"] for row in rows.values() if row["active"]}
            for key, rows in self.by_key.items()
        }
        book = {key: moves for key, moves in book.items() if moves}
        seen = set()
        for item in ordinary:
            identity = candidate_id(item["key"], item["move"])
            row = self.state.get(identity)
            if row is None:
                row = {
                    "id": identity,
                    "key": item["key"],
                    "move": item["move"],
                    "generation": 0,
                    "active": False,
                    "wins": 0,
                    "lifetime_wins": 0,
                    "games": 0,
                    "visits": 0,
                    "weight": 1.0,
                    "legacy_count": 0,
                    "reason": "observed",
                }
                self.state[identity] = row
                self.by_key.setdefault(item["key"], {})[identity] = row
            row["visits"] += 1
            if identity not in seen:
                row["games"] += 1
                if winner is not None and item["key"].split()[1] == winner:
                    row["lifetime_wins"] += 1
                seen.add(identity)
        if winner is not None:
            legacy_learn(book, ordinary, "w" if winner == "b" else "b")
            for identity in touched:
                row = self.state[identity]
                count = book.get(row["key"], {}).get(row["move"])
                if count is not None:
                    if not row["active"]:
                        row.update(
                            generation=row["generation"] + 1,
                            reason="legacy_win_restore",
                        )
                        row.pop("disabled_by", None)
                    row.update(active=True, wins=count)
                elif row["active"]:
                    row.update(active=False, reason="lost", disabled_by=game_id)
        return {
            identity: deepcopy(self.state[identity])
            for identity in touched
            if self.state[identity] != before.get(identity)
        }
