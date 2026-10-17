"""Fault injection against the public runner's actual commit/recovery path."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
from core.runner import Runner
from core.store import Store
from test_app import upstream_oracle

ROOT = Path(__file__).resolve().parents[1]


class RecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = Path(self.tmp.name)
        settings = app.checked_settings(app.DEFAULTS | {
            "engine": sys.executable,
            "engine_arguments": [str(ROOT / "tests/fake_engine.py"), "game"],
            "nodes": 10,
            "threads": 1,
            "hash_mb": 16,
            "target_black_streak": 2,
            "tasks": [{"label": "test", "moves": []}],
        })
        path = directory / "campaign.json"
        path.write_text(json.dumps(app.campaign(settings, directory)), "utf-8")
        self.config = app.load_config(path)
        app.initialize_book(self.config)
        self.store = Store(directory / "runtime/state.sqlite3")
        self.runner = Runner(self.config, self.store)

    def tearDown(self):
        self.runner.engine.close()
        self.store.close()
        self.tmp.cleanup()

    def test_retry_after_successful_commit_does_not_update_book_twice(self):
        commit = self.store.commit
        injected = False

        def commit_then_fail(kind, payload, identity):
            nonlocal injected
            result = commit(kind, payload, identity)
            if kind == "game_finished" and not injected:
                injected = True
                raise OSError("simulated lost commit acknowledgement")
            return result

        with patch.object(self.store, "commit", side_effect=commit_then_fail):
            self.assertEqual(self.runner.run(1)[:2], (1, 1))
        self.assertTrue(injected)
        games = self.store.db.execute("SELECT payload FROM games").fetchall()
        self.assertEqual(len(games), 1)
        game = json.loads(games[0][0])
        expected = upstream_oracle({}, game["selections"], "w")
        actual = {}
        for row in self.store.candidates().values():
            if row["active"]:
                actual.setdefault(row["key"], {})[row["move"]] = row["wins"]
        self.assertEqual(actual, expected)
        task = self.store.service()["sequential_study"]["tasks"][0]
        self.assertEqual((task["completed"], task["black_streak"]), (1, 1))

    def test_interrupted_checkpoint_never_updates_book_or_winning_streak(self):
        self.runner.run(1)
        completed = json.loads(self.store.db.execute("SELECT payload FROM games").fetchone()[0])
        partial = deepcopy(completed)
        partial.update(
            game_id="interrupted-checkpoint",
            completed=False,
            reason=None,
            rule_result="unresolved",
            research_result="unresolved",
            moves=completed["moves"][:2],
            selections=completed["selections"][:2],
            total_plies=2,
        )
        for key in ("terminal", "terminal_info", "finished_unix"):
            partial.pop(key, None)
        before = self.store.candidates()
        self.runner.durable.checkpoint(partial)
        self.assertEqual(self.runner.recover_interrupted(), partial["game_id"])
        self.assertIsNone(self.runner.recover_interrupted())
        self.assertEqual(self.store.candidates(), before)
        task = self.store.service()["sequential_study"]["tasks"][0]
        self.assertEqual((task["completed"], task["black_streak"], task["interrupted"]), (1, 1, 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2)

    def test_journal_replay_preserves_games_book_progress_and_accepted_settings(self):
        self.runner.controller.initialize()
        self.runner.controller.accept({
            "schema_version": 1,
            "task_id": "test-settings",
            "kind": "control",
            "target": {"campaign_id": self.config["campaign_id"]},
            "operation": "configure",
            "expected_settings_revision": 0,
            "changes": {"nodes": 12},
        }, "test-request")
        self.runner.run(1)
        log = Path(self.tmp.name) / "replay.jsonl"
        self.store.replicate(log)
        restored = Store(Path(self.tmp.name) / "restored/state.sqlite3")
        try:
            self.assertEqual(restored.restore(log), self.store.current_seq())
            for table in ("candidates", "games", "events", "tasks", "metadata", "game_edges", "game_stats"):
                self.assertEqual(
                    sorted(map(tuple, restored.db.execute("SELECT * FROM " + table))),
                    sorted(map(tuple, self.store.db.execute("SELECT * FROM " + table))),
                    table,
                )
            settings = restored.service()["runtime_settings"]["values"]
            self.assertEqual((settings["nodes"], settings["repetition_limit"]), (12, 2))
            with self.assertRaisesRegex(ValueError, "empty database"):
                restored.restore(log)
        finally:
            restored.close()

    def test_damaged_recovery_log_never_partially_changes_empty_database(self):
        self.runner.run(1)
        log = Path(self.tmp.name) / "replay.jsonl"
        self.store.replicate(log)
        original = log.read_text("utf-8")
        for name, text in (("torn", original[:-2]), ("changed", original.replace('"seq":1', '"seq":9', 1))):
            with self.subTest(name=name):
                bad_log = Path(self.tmp.name) / (name + ".jsonl")
                bad_log.write_text(text, "utf-8")
                restored = Store(Path(self.tmp.name) / name / "state.sqlite3")
                try:
                    with self.assertRaises(ValueError):
                        restored.restore(bad_log)
                    self.assertEqual(restored.current_seq(), 0)
                    self.assertFalse(restored.candidates())
                    self.assertEqual(restored.db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 0)
                finally:
                    restored.close()


if __name__ == "__main__":
    unittest.main()
