"""Incremental viewer topology with Japanese move labels and transpositions."""

import cshogi


def move_label(board, move):
    parsed = board.move_from_usi(move)
    names = {
        "P": "歩",
        "L": "香",
        "N": "桂",
        "S": "銀",
        "G": "金",
        "B": "角",
        "R": "飛",
    }
    if "*" in move:
        name = names[move[0]]
    else:
        square = cshogi.move_from(parsed)
        name = {
            1: "歩",
            2: "香",
            3: "桂",
            4: "銀",
            5: "角",
            6: "飛",
            7: "金",
            8: "玉",
            9: "と",
            10: "成香",
            11: "成桂",
            12: "成銀",
            13: "馬",
            14: "龍",
        }[board.pieces[square] & 15]
    return (
        ("▲" if board.turn == cshogi.BLACK else "△")
        + "１２３４５６７８９"[int(move[2]) - 1]
        + "一二三四五六七八九"[ord(move[3]) - 97]
        + name
        + ("打" if "*" in move else "成" if move.endswith("+") else "")
    )


class ViewTree:
    def __init__(self, config):
        from .runner import position_board

        board, origin, prefix, *_ = position_board(config["start_position"])
        self.origin, self.prefix = origin, prefix
        self.base = board.move_number - 1
        self.nodes = [
            dict(
                id=0,
                parent=-1,
                move="開始局面",
                label="開始局面",
                sfen=board.sfen(),
                main=0,
                count=0,
                b=0,
                w=0,
                children={},
                original=True,
            )
        ]
        self.positions = {" ".join(board.sfen().split()[:3]): 0}
        self.completed = 0
        self.black_streak = 0
        self.best_black_streak = 0
        self.last_games = {}

    def walk(self, moves, reference=False):
        current = 0
        seen = {0}
        for move in moves:
            parent = self.nodes[current]
            child = parent["children"].get(move)
            if child is None:
                board = cshogi.Board(parent["sfen"])
                parsed = board.move_from_usi(move)
                if not board.is_legal(parsed):
                    raise ValueError("Illegal viewer move")
                label = move_label(board, move)
                board.push(parsed)
                child = len(self.nodes)
                position = " ".join(board.sfen().split()[:3])
                main = self.positions.setdefault(position, child)
                self.nodes.append(
                    dict(
                        id=child,
                        parent=current,
                        move=move,
                        label=label,
                        sfen=board.sfen(),
                        main=main,
                        count=0,
                        b=0,
                        w=0,
                        children={},
                        original=reference,
                    )
                )
                parent["children"][move] = child
            seen.add(child)
            current = self.nodes[child]["main"]
            if reference:
                self.nodes[child]["original"] = True
        return current, seen

    def path(self, index):
        moves = []
        while index > 0:
            node = self.nodes[index]
            moves.append(node["move"])
            index = node["parent"]
        return " ".join(reversed(moves))

    def add(
        self,
        moves,
        result,
        identity,
        worker=1,
        origin=None,
        reason=None,
        focus_id=None,
        prefix_length=None,
        stop_ply=None,
    ):
        _, seen = self.walk(moves[len(self.prefix) : stop_ply])
        for index in seen:
            node = self.nodes[index]
            node["count"] += 1
            node["b"] += int(result == "black_win")
            node["w"] += int(result == "white_win")
        self.completed += 1
        self.black_streak = self.black_streak + 1 if result == "black_win" else 0
        self.best_black_streak = max(self.best_black_streak, self.black_streak)
        self.last_games[str(worker)] = dict(
            game_id=identity,
            moves=moves,
            origin=origin or self.origin,
            prefix_length=len(self.prefix) if prefix_length is None else prefix_length,
            reason=reason,
            rule_result=result,
            focus_id=focus_id,
        )

    def export(self):
        # Parents precede children, including proxy entrances to shared bodies.
        return dict(
            count=self.completed,
            completed=self.completed,
            black_streak=self.black_streak,
            best_black_streak=self.best_black_streak,
            base=self.base,
            origin=self.origin,
            prefix=self.prefix,
            last_games=self.last_games,
            nodes=[
                {k: v for k, v in node.items() if k != "children"}
                for node in self.nodes
            ],
        )
