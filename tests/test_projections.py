import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
from core.learning import read_book
from core.publication import Publications
from core.sfen_archive import SfenArchive
from core.store import Store, atomic_write
from core.usi import Engine, EngineError


ROOT = Path(__file__).resolve().parents[1]


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.config = dict(
            campaign_id="test",
            runtime_state_dir=str(self.directory / "runtime"),
            sfen_archive=str(self.directory / "games.sfen"),
            start_position="position startpos",
        )
        self.store = Store(self.directory / "runtime/state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def game(self, identity, completed=True, **changes):
        game = dict(
            game_id=identity,
            campaign_id="test",
            origin="startpos",
            moves=["7g7f", "3c3d"],
            completed=completed,
            reason="resign",
            rule_result="black_win",
            research_result="black_win",
            prefix_length=0,
            selections=[dict(key="test b -", move="7g7f", info="x" * 32768)],
        )
        game.update(changes)
        self.store.commit("game_finished", {"game": game}, identity)
        return game

    def test_archive_only_appends_completed_and_recovers_torn_tail(self):
        self.game("one")
        archive = SfenArchive(self.config)
        archive.publish(self.store)
        path = Path(self.config["sfen_archive"])
        before = path.read_bytes()
        checkpoint = self.directory / "runtime/workers/1/in_progress.json"
        live = dict(
            game_id="live", campaign_id="test", origin="startpos", moves=["2g2f"]
        )
        atomic_write(checkpoint, json.dumps(live))
        queries = []
        self.store.db.set_trace_callback(queries.append)
        with patch("core.sfen_archive.atomic_write", wraps=atomic_write) as write:
            archive.publish(self.store)
            live["moves"].append("8c8d")
            atomic_write(checkpoint, json.dumps(live))
            archive.publish(self.store)
            self.assertTrue(
                all(
                    Path(call.args[0]).name
                    in {"games.live.sfen", "games.live.index.json"}
                    for call in write.call_args_list
                )
            )
        self.assertFalse(any("json_extract" in q.lower() for q in queries))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn(b"2g2f 8c8d", path.with_suffix(".live.sfen").read_bytes())
        self.game("two")
        with patch(
            "core.sfen_archive.atomic_write",
            side_effect=OSError("index replacement failed"),
        ):
            with self.assertRaises(OSError):
                archive.publish(self.store)
        self.assertGreater(len(path.read_bytes()), len(before))
        # Simulate restart after SFEN fsync but before the new index commit.
        recovered = SfenArchive(self.config)
        recovered.publish(self.store)
        self.assertEqual(len(path.read_text().splitlines()), 2)
        self.assertEqual(path.read_bytes(), before + before)
        self.game("aborted", completed=False)
        recovered.publish(self.store)
        self.assertEqual(len(path.read_text().splitlines()), 2)
        index = json.loads(path.with_suffix(".index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["sfen_bytes"], path.stat().st_size)
        self.assertEqual(
            index["sfen_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertNotIn("selections", index["games"][0])

    def test_old_or_damaged_archive_is_rebuilt_from_sqlite(self):
        path = Path(self.config["sfen_archive"])
        path.write_text("obsolete\n", encoding="utf-8")
        atomic_write(path.with_suffix(".index.json"), '{"schema_version":1}')
        archive = SfenArchive(self.config)
        archive.publish(self.store)
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(
            json.loads(path.with_suffix(".index.json").read_text())["schema_version"], 2
        )
        self.game("one")
        archive.publish(self.store)
        expected = path.read_bytes()
        path.write_bytes(b"bad data")
        SfenArchive(self.config).publish(self.store)
        self.assertEqual(path.read_bytes(), expected)

    def test_public_cache_incremental_projection_and_no_search_info_reads(self):
        self.game("one")
        publications = Publications()
        queries = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("core.publication.sqlite3.connect", side_effect=traced):
            first = publications.read(self.config, "tree")
            self.assertEqual(json.loads(first.body)["count"], 1)
            queries.clear()
            for _ in range(5):
                self.assertIs(publications.read(self.config, "tree"), first)
            self.assertFalse(any("from games" in q.lower() for q in queries))
            self.store.commit("control", {"service": {"paused": True}}, "control")
            # Non-game events must not flatten/compress the existing tree again.
            with patch(
                "core.publication.Response.make", side_effect=AssertionError("rebuilt")
            ):
                self.assertIs(publications.read(self.config, "tree"), first)
            self.game(
                "two",
                reason="max_total_plies",
                rule_result="unresolved",
                research_result="unresolved",
            )
            second = publications.read(self.config, "tree")
            self.assertEqual(json.loads(second.body)["count"], 2)
            text = publications.read(self.config, "games.sfen").body.decode()
            self.assertEqual(len(text.splitlines()), 2)
            self.assertTrue(text.splitlines()[1].endswith("max_moves"))
            self.assertEqual(gzip.decompress(second.compressed), second.body)
            self.assertFalse(
                any("select payload from games" in q.lower() for q in queries)
            )

    def test_full_tree_keeps_input_route_and_task_tree_owns_continuation(self):
        prefix = ["7g7f", "3c3d"]
        route = prefix + ["2g2f", "8c8d"]
        config = self.config | {
            "start_position": "position startpos moves " + " ".join(prefix),
            "sequential_study": {
                "target_black_streak": 100,
                "focuses": [{"id": "task-01", "label": "課題1", "moves": route[2:]}],
            },
        }
        self.game(
            "task-game",
            moves=route + ["2f2e"],
            prefix_length=4,
            focus={"id": "task-01"},
        )
        publication = Publications()
        full = json.loads(publication.read(config, "tree").body)
        task = json.loads(publication.read(config, "task:task-01").body)
        self.assertEqual(full["base"], 0)
        self.assertEqual(full["prefix"], [])
        self.assertEqual([n["move"] for n in full["nodes"][1:]], route)
        self.assertEqual(full["tasks"][0]["node"], " ".join(route))
        self.assertEqual(full["tasks"][0]["ply"], 4)
        self.assertEqual(task["base"], 4)
        self.assertEqual(task["prefix"], route)
        self.assertEqual([n["move"] for n in task["nodes"][1:]], ["2f2e"])

    def test_start_position_alone_also_creates_a_view_boundary(self):
        prefix = ["7g7f", "3c3d"]
        config = self.config | {
            "start_position": "position startpos moves " + " ".join(prefix)
        }
        self.game(
            "start-only",
            moves=prefix + ["2g2f"],
            prefix_length=2,
            focus={"id": "anchor"},
        )
        publication = Publications()
        full = json.loads(publication.read(config, "tree").body)
        task = json.loads(publication.read(config, "task:anchor").body)
        self.assertEqual([n["move"] for n in full["nodes"][1:]], prefix)
        self.assertEqual(full["tasks"][0]["node"], " ".join(prefix))
        self.assertEqual(task["prefix"], prefix)
        self.assertEqual(task["nodes"][1]["move"], "2g2f")
        self.assertEqual(full["tasks"][0]["black_streak"], 1)
        self.assertNotIn("sequential_study", config)

    def test_initial_and_sfen_origins_do_not_invent_a_reference_history(self):
        import cshogi

        board = cshogi.Board()
        board.push_usi("7g7f")
        board.push_usi("3c3d")
        for origin, ply in (("startpos", 0), ("sfen " + board.sfen(), 2)):
            config = self.config | {"start_position": "position " + origin}
            publication = Publications()
            full = json.loads(publication.read(config, "tree").body)
            self.assertEqual(len(full["nodes"]), 1)
            self.assertEqual(full["tasks"][0]["node"], "")
            self.assertEqual(full["tasks"][0]["ply"], ply)

    def test_concurrent_downloads_share_one_build_and_do_not_lock_control(self):
        publications = Publications()
        entered, release = threading.Event(), threading.Event()
        application = app.Application(self.directory / "web")
        application.config = self.config
        application.publications = publications
        results = []

        def slow_book(_rows):
            entered.set()
            release.wait(5)
            return "#YANEURAOU-DB2016 1.00\n", 0

        with patch("core.publication.book_text", side_effect=slow_book) as build:
            threads = [
                threading.Thread(
                    target=lambda: results.append(application.publication("book.db"))
                )
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(entered.wait(3))
                # A request building a public download holds no control guard.
                self.assertTrue(application.guard.acquire(timeout=0.5))
                application.guard.release()
                self.assertEqual(application.status()["phase"], "stopped")
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(value is results[0] for value in results))


class CompatibilityTests(unittest.TestCase):
    def test_four_column_book_and_clear_parse_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.db"
            key = " ".join(app.cshogi.Board().sfen().split()[:3])
            path.write_text(
                "\ufeff#YANEURAOU-DB2016 1.00\nsfen " + key + " 1\n7g7f none 0 32\n",
                encoding="utf-8",
            )
            self.assertEqual(read_book(path), {key: {"7g7f": 1}})
            self.assertEqual(app.load_book({"book": str(path)}), {key: {"7g7f": 1}})
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                read_book(path)

    def test_missing_current_campaign_starts_without_discarding_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            saved = json.dumps(
                {"settings": app.DEFAULTS, "config": str(directory / "missing.json")}
            )
            (directory / "current.json").write_text(saved, encoding="utf-8")
            with self.assertLogs(level="WARNING"):
                application = app.Application(directory)
            self.assertIsNone(application.config)
            self.assertTrue(application.startup_warning)
            self.assertEqual(
                (directory / "current.json").read_text(encoding="utf-8"), saved
            )

    def test_reserved_hash_and_optional_options_on_minimal_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "usi.jsonl"
            engine = Engine(
                [sys.executable, str(ROOT / "tests/fake_engine.py"), "minimal"],
                options={"USI_Hash": 16, "Threads": 1, "ResignValue": 300},
                log_path=log,
            )
            try:
                engine.start()
                text = log.read_text(encoding="utf-8")
                self.assertIn("setoption name USI_Hash value 16", text)
                self.assertNotIn("setoption name Threads", text)
                self.assertNotIn("setoption name ResignValue", text)
                self.assertIn('"warning"', text)
                engine.options = {"MisspelledOption": 1}
                with self.assertRaises(EngineError):
                    engine.start()
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
