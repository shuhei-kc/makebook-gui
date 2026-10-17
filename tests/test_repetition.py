"""Twofold endings through the actual runner, update, export and recovery validator."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import app
from core.durable_games import validate_record
from core.runner import Runner
from core.sfen_archive import completed_line
from core.store import Store
from test_app import upstream_oracle

ROOT = Path(__file__).resolve().parents[1]
CHECK_START = "4k4/9/4R4/9/9/9/9/9/K8 w - 1"
WHITE_CHECK_START = "8k/9/9/9/9/9/4r4/9/4K4 b - 1"
CHECK_CYCLE = "5a4a 5c4c 4a5a 4c5c"


class RepetitionTests(unittest.TestCase):
    def run_game(self, mode, start, draw, expected, length, reason):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            settings = app.checked_settings(app.DEFAULTS | {
                "engine": sys.executable,
                "engine_arguments": [str(ROOT / "tests/fake_engine.py"), mode],
                "position": start,
                "draw": draw,
                "nodes": 10,
                "threads": 1,
                "hash_mb": 16,
                "save_search_details": True,
                "tasks": [{"label": "test", "moves": []}],
            })
            path = directory / "campaign.json"
            path.write_text(json.dumps(app.campaign(settings, directory)), "utf-8")
            config = app.load_config(path)
            app.initialize_book(config)
            store = Store(directory / "runtime/state.sqlite3")
            runner = Runner(config, store)
            try:
                before = {}
                for row in store.candidates().values():
                    if row["active"]:
                        before.setdefault(row["key"], {})[row["move"]] = row["wins"]
                self.assertEqual(runner.run(1)[:2], (1, 1))
                game = json.loads(store.db.execute("SELECT payload FROM games").fetchone()[0])
                self.assertEqual(game["reason"], reason)
                self.assertEqual(game["rule_result"], "unresolved")
                self.assertEqual(game["research_result"], expected)
                self.assertEqual(len(game["selections"]), length)
                self.assertEqual(len(game["moves"]), game["prefix_length"] + length)
                if not game["prefix_length"]:
                    sent = [json.loads(line)["line"] for line in
                            (directory / "runtime/usi.jsonl").read_text("utf-8").splitlines()
                            if json.loads(line)["direction"] == "in"]
                    positions = [line for line in sent if line.startswith("position ")]
                    self.assertEqual([int(line.split()[-1]) for line in positions],
                                     list(range(1, length + 1)))
                    self.assertTrue(all(line.startswith("position sfen ") and " moves " not in line
                                        for line in positions))
                    self.assertEqual(sent.count("isready"), 1)
                    self.assertNotIn("usinewgame", sent)
                validate_record(game, config)
                task = store.service()["sequential_study"]["tasks"][0]
                self.assertEqual(task["black_streak"], 0)
                self.assertEqual(task["results"], {"unresolved": 1})
                actual = {}
                for row in store.candidates().values():
                    if row["active"]:
                        actual.setdefault(row["key"], {})[row["move"]] = row["wins"]
                loser = "w" if expected == "black_win" else "b"
                self.assertEqual(actual, upstream_oracle(before, game["selections"], loser))
                self.assertTrue(completed_line(game).endswith(" rep_draw"))
                bad = deepcopy(game)
                bad["rule_result"] = expected
                with self.assertRaises(ValueError):
                    validate_record(bad, config)
                bad = deepcopy(game)
                bad["research_result"] = "white_win" if expected == "black_win" else "black_win"
                with self.assertRaises(ValueError):
                    validate_record(bad, config)
            finally:
                runner.engine.close()
                store.close()

    def test_checking_black_loses_at_two_even_when_draw_policy_favors_black(self):
        self.run_game("checking_loop", "position sfen " + CHECK_START, "w",
                      "white_win", 4, "checking_loop_abort")

    def test_checking_white_loses_at_two_even_when_draw_policy_favors_white(self):
        self.run_game("white_checking_loop", "position sfen " + WHITE_CHECK_START, "b",
                      "black_win", 4, "checking_loop_abort")

    def test_checking_loss_also_overrides_ignored_draws(self):
        self.run_game("checking_loop", "position sfen " + CHECK_START, "ignore",
                      "white_win", 4, "checking_loop_abort")

    def test_a_nonchecking_move_breaks_continuous_check(self):
        # The first repeated position is the position after White's first
        # move, reached again at ply five, before the root would recur.
        self.run_game("broken_checking_loop", "position sfen " + CHECK_START, "w",
                      "black_win", 5, "loop_abort")

    def test_reference_checking_cycle_does_not_end_game_before_play(self):
        self.run_game("checking_loop", "position sfen " + CHECK_START + " moves " + CHECK_CYCLE,
                      "w", "white_win", 4, "checking_loop_abort")


if __name__ == "__main__":
    unittest.main()
