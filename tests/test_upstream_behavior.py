"""Compare behavior with the pinned dependency, not a restated specification."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app
import cshogi
from core.learning import key_of
from core.live_settings import current, effective_config
from core.runner import position_board, repetition

spec = importlib.util.spec_from_file_location(
    "pinned_makebook2_board", ROOT / "docs/upstream/Board.py"
)
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)


class UpstreamBehaviorTests(unittest.TestCase):
    def compare_route(self, initial, moves):
        board = cshogi.Board(initial)
        original = upstream.Board("sfen " + initial)
        keys, checks = [key_of(board)], []
        for move in moves:
            self.assertEqual(
                repetition(keys, checks) is not None, original.is_sennichite()
            )
            parsed = board.move_from_usi(move)
            self.assertTrue(board.is_legal(parsed), move)
            side = "b" if board.turn == cshogi.BLACK else "w"
            board.push(parsed)
            original.push(move)
            keys.append(key_of(board))
            checks.append((side, board.is_check()))
            self.assertEqual(key_of(board), " ".join(original.sfen().split()[1:4]))
        self.assertEqual(
            repetition(keys, checks) is not None, original.is_sennichite()
        )
        return keys, checks

    def test_first_repetition_matches_upstream_board(self):
        keys, checks = self.compare_route(
            cshogi.STARTING_SFEN, "5i5h 5a5b 5h5i 5b5a".split()
        )
        self.assertEqual(repetition(keys, checks), ("repetition", "draw"))

    def test_checking_repetition_identifies_the_checking_side_at_two(self):
        initial = "4k4/9/4R4/9/9/9/9/9/K8 w - 1"
        cycle = "5a4a 5c4c 4a5a 4c5c".split()
        keys, checks = self.compare_route(initial, cycle)
        self.assertEqual(repetition(keys, checks), ("perpetual_check", "white_win"))
        self.assertIsNone(repetition(keys, checks, 4))
        _, _, _, keys, checks = position_board(
            "sfen " + initial + " moves " + " ".join(cycle * 3)
        )
        self.assertEqual(repetition(keys, checks, 4), ("perpetual_check", "white_win"))

    def test_turn_and_hands_match_upstream_repetition_identity(self):
        initial = "4k4/9/9/9/9/9/9/9/4K4 b P 1"
        first = upstream.Board("sfen " + initial)
        for variant in [initial.replace(" b ", " w "), initial.replace(" P ", " - ")]:
            original = upstream.Board("sfen " + variant)
            original.history.insert(0, first.board[11:115].copy())
            self.assertFalse(original.is_sennichite())
            self.assertIsNone(
                repetition([key_of(cshogi.Board(initial)), key_of(cshogi.Board(variant))], [])
            )

    def test_legal_routes_captures_and_promotions_match_upstream(self):
        self.compare_route(
            cshogi.STARTING_SFEN, "7g7f 3c3d 8h2b+ 3a2b".split()
        )
        rng = random.Random(41)
        for _ in range(16):
            board, moves = cshogi.Board(), []
            for _ in range(40):
                choices = list(board.legal_moves)
                if not choices:
                    break
                move = rng.choice(choices)
                moves.append(cshogi.move_to_usi(move))
                board.push(move)
            self.compare_route(cshogi.STARTING_SFEN, moves)

    def test_new_campaign_and_resume_keep_the_agreed_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            settings = app.checked_settings(app.DEFAULTS | {"engine": sys.executable})
            config_path = directory / "campaign.json"
            config_path.write_text(json.dumps(app.campaign(settings, directory)), "utf-8")
            config = app.load_config(config_path)
            self.assertEqual(config["repetition_limit"], 2)
            self.assertEqual(config["max_total_plies"], 256)
            self.assertEqual(
                config["research_outcomes"],
                {"repetition": "white_win", "max_total_plies": "white_win"},
            )
            self.assertEqual(
                config["engine"]["usi_options"],
                {"Threads": 4, "USI_Hash": 1024, "ResignValue": 300},
            )
            self.assertFalse(config["save_search_details"])
            self.assertEqual(settings["target_black_streak"], 100)
            saved = current(config, {})
            self.assertEqual(saved["values"]["repetition_limit"], 2)
            self.assertEqual(
                effective_config(config, current(config, {"runtime_settings": saved})["values"])[
                    "repetition_limit"
                ], 2
            )
            # Old saved records really used four occurrences. Preserve their
            # frozen condition; never relabel their results as twofold games.
            old = deepcopy(saved)
            old["values"]["repetition_limit"] = 4
            self.assertEqual(
                current(config, {"runtime_settings": old})["values"]["repetition_limit"], 4
            )
            del old["values"]["repetition_limit"]
            self.assertEqual(
                current(config, {"runtime_settings": old})["values"]["repetition_limit"], 4
            )


if __name__ == "__main__":
    unittest.main()
