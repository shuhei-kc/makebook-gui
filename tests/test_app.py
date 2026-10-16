import ast
from copy import deepcopy
from http.client import HTTPConnection
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from core.learning import Candidates, legacy_learn
from core.store import Store


def upstream_oracle(book, selections, loser):
    source = ast.parse(
        (ROOT / "docs/upstream/makebook2.py").read_text(encoding="utf-8-sig")
    )
    loops = [
        n
        for n in ast.walk(source)
        if isinstance(n, ast.For) and ast.unparse(n.iter) == "reversed(tempbook)"
    ]
    # Importing the original script would prompt and start engines. Compile only
    # its unchanged learning loop instead of reimplementing the expected result.
    code = compile(
        ast.Module(body=[loops[0]], type_ignores=[]), "makebook2-learning", "exec"
    )
    key = lambda x: "sfen " + x + " 0"
    obj = SimpleNamespace(book={key(k): deepcopy(v) for k, v in book.items()})
    env = dict(
        Shogi=obj,
        tempbook=[(key(x["key"]), x["move"]) for x in selections],
        loseturn=loser,
        bookpass=0,
    )
    exec(code, env)
    return {" ".join(k.split()[1:-1]): v for k, v in obj.book.items()}


class LearningTests(unittest.TestCase):
    def test_pinned_upstream_bytes(self):
        directory = ROOT / "docs/upstream"
        source = json.loads((directory / "source.json").read_text(encoding="utf-8"))
        for name, expected in source["sha256"].items():
            self.assertEqual(
                hashlib.sha256((directory / name).read_bytes()).hexdigest(), expected
            )

    def test_initial_reference_and_import_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            initial = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -"
            book = directory / "input.db"
            board = app.cshogi.Board()
            board.push_usi("7g7f")
            board.push_usi("3c3d")
            other = " ".join(board.sfen().split()[:3])
            book.write_text(
                "#YANEURAOU-DB2016 1.00\nsfen "
                + initial
                + " 0\n7g7f none 0 32 999\n2g2f none 0 32 777\nsfen "
                + other
                + " 0\n2g2f none 0 32 888\n",
                encoding="utf-8",
            )
            settings = app.checked_settings(
                app.DEFAULTS
                | {
                    "engine": sys.executable,
                    "book": str(book),
                    "position": "position startpos moves 7g7f 3c3d",
                }
            )
            for fixed in [False, True]:
                settings["fixed_side"] = "b" if fixed else ""
                session = directory / ("fixed" if fixed else "learned")
                session.mkdir()
                cfg_path = session / "campaign.json"
                cfg_path.write_text(
                    json.dumps(app.campaign(settings, session)), encoding="utf-8"
                )
                config = app.load_config(cfg_path)
                app.initialize_book(config)
                store = Store(Path(config["runtime_state_dir"]) / "state.sqlite3")
                try:
                    state = store.candidates()
                    if fixed:
                        self.assertEqual(
                            sorted(r["wins"] for r in state.values()), [1000, 1000]
                        )
                        self.assertEqual(
                            config["fixed_rules"][0]["moves"][0]["usi"], "7g7f"
                        )
                    else:
                        self.assertEqual(
                            sorted(r["wins"] for r in state.values()), [1, 1000, 1000]
                        )
                        self.assertEqual(
                            [r["move"] for r in state.values() if r["key"] == initial],
                            ["7g7f"],
                        )
                finally:
                    store.close()

    def test_literal_upstream_loop_randomized(self):
        rng = random.Random(54)
        keys = [f"board{i} {side} -" for i in range(7) for side in ("b", "w")]
        for _ in range(500):
            before = {
                k: {f"move{m}": rng.randint(1, 1000) for m in range(rng.randint(1, 3))}
                for k in rng.sample(keys, rng.randint(0, len(keys)))
            }
            selections = [
                dict(
                    key=rng.choice(keys),
                    move="move" + str(rng.randrange(4)),
                    source=rng.choice(["engine", "book", "fixed"]),
                )
                for _ in range(rng.randint(1, 35))
            ]
            loser = rng.choice(["b", "w"])
            actual = deepcopy(before)
            legacy_learn(actual, selections, loser)
            self.assertEqual(actual, upstream_oracle(before, selections, loser))

    def test_public_metadata_matches_upstream_even_with_old_generations(self):
        learner = Candidates()
        original = {"A b -": {"x": 10}, "B w -": {"x": 4, "y": 7}, "C w -": {"z": 8}}
        for key, moves in original.items():
            for move, count in moves.items():
                row = learner.register(key, move)
                row.update(wins=count, generation=9)
        selections = [
            dict(key="C w -", move="z", source="fixed", generation=1),
            dict(key="A b -", move="x", source="book", generation=1),
            dict(key="B w -", move="x", source="engine", generation=None),
        ]
        learner.learn(selections, "b", "test-game")
        active = {
            k: {r["move"]: r["wins"] for r in rows.values() if r["active"]}
            for k, rows in learner.by_key.items()
        }
        active = {k: v for k, v in active.items() if v}
        self.assertEqual(active, upstream_oracle(original, selections, "w"))
        self.assertEqual(active["C w -"]["z"], 9)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = app.Application(Path(self.tmp.name))
        self.servers = []
        for admin in [True, False]:
            server = app.ThreadingHTTPServer(
                ("127.0.0.1", 0), app.handler_for(self.app, admin)
            )
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.app.child and self.app.child.poll() is None:
            self.app.control("stop_after_games")
            try:
                self.app.child.wait(timeout=10)
            except app.subprocess.TimeoutExpired:
                self.app.child.terminate()
                self.app.child.wait(timeout=5)
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, public=False, valid=True):
        port = self.servers[int(public)].server_port
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {}
        if body is not None:
            headers = {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}"
                if valid
                else "https://other.example",
                "X-Makebook-Token": self.app.token,
            }
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers)
        result = conn.getresponse()
        code, data = result.status, result.read()
        conn.close()
        return code, data

    def test_public_is_read_only_and_does_not_expose_settings(self):
        self.assertEqual(self.request("/api/start", "POST", {}, public=True)[0], 405)
        self.assertEqual(self.request("/api/settings", public=True)[0], 404)
        self.assertEqual(self.request("/api/start", "POST", {}, valid=False)[0], 403)
        self.assertEqual(self.request("/../app.py", public=True)[0], 404)
        self.assertEqual(self.request("/manage.js", public=True)[0], 404)
        html = self.request("/")[1].decode()
        self.assertLess(html.index('src="/app.js"'), html.index('src="/connect.js"'))
        self.assertLess(html.index('src="/connect.js"'), html.index('src="/manage.js"'))
        self.assertIn(b"settings-dialog", html.encode())

    def test_invalid_json_shape_and_bad_book_return_400(self):
        for path, value in [
            ("/api/control", []),
            ("/api/start", None),
            ("/api/control", {"operation": []}),
        ]:
            # The request helper omits a body for None; use another scalar here.
            code, data = self.request(path, "POST", value if value is not None else 1)
            self.assertEqual(code, 400, data)
            self.assertIn("error", json.loads(data))
        book = Path(self.tmp.name) / "invalid.db"
        book.write_text(
            "sfen "
            + " ".join(app.cshogi.Board().sfen().split()[:3])
            + " 1\n7g7f none\n",
            encoding="utf-8",
        )
        code, data = self.request(
            "/api/start",
            "POST",
            app.DEFAULTS | {"engine": sys.executable, "book": str(book)},
        )
        self.assertEqual(code, 400, data)
        self.assertIn("2行目", json.loads(data)["error"])
        with patch.object(
            self.app, "start", side_effect=RuntimeError("private detail")
        ):
            with self.assertLogs(level="ERROR"):
                code, data = self.request("/api/start", "POST", {})
            self.assertEqual(code, 500)
            self.assertNotIn(b"private detail", data)

    def test_loading_book_does_not_block_status(self):
        entered, release = threading.Event(), threading.Event()

        def slow_load(_settings):
            entered.set()
            release.wait(5)
            raise ValueError("test validation stopped")

        result = []
        with patch.object(app, "load_book", side_effect=slow_load):
            thread = threading.Thread(
                target=lambda: result.append(
                    self.request(
                        "/api/start", "POST", app.DEFAULTS | {"engine": sys.executable}
                    )
                )
            )
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                before = time.monotonic()
                self.assertEqual(self.request("/api/state", public=True)[0], 200)
                self.assertLess(time.monotonic() - before, 1)
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(result[0][0], 400)

    def test_sequential_tasks_and_streak_target_finish_without_overshoot(self):
        settings = app.DEFAULTS | {
            "engine": sys.executable,
            "engine_arguments": [str(ROOT / "tests/fake_engine.py"), "black_win"],
            "nodes": 10,
            "threads": 1,
            "hash_mb": 16,
            "parallel": 3,
            "target_black_streak": 2,
            "tasks": [
                {"label": "課題1", "moves": []},
                {"label": "課題2", "moves": "position startpos moves 7g7f 3c3d"},
            ],
        }
        self.app.start(settings)
        self.wait_for_games(4)
        self.app.child.wait(timeout=10)
        state = self.app.status()
        self.assertEqual(state["completed_games"], 4)
        self.assertTrue(state["sequential_study"]["done"])
        self.assertEqual(
            [t["black_streak"] for t in state["sequential_study"]["tasks"]], [2, 2]
        )
        self.assertEqual(
            json.loads(self.request("/api/tree", public=True)[1])["tasks"][1]["ply"], 2
        )
        for identity in ("task-01", "task-02"):
            code, body = self.request("/api/task_tree?id=" + identity, public=True)
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body)["count"], 2)
            code, body = self.request("/api/task_sfen?id=" + identity, public=True)
            self.assertEqual(code, 200, body)
            self.assertEqual(len(body.decode().splitlines()), 2)
        self.assertEqual(self.request("/api/task_tree?id=unknown", public=True)[0], 400)

    def test_fourfold_repetition_excludes_reference_moves(self):
        prefix = "5i5h 5a5b 5h5i 5b5a " * 3
        settings = app.DEFAULTS | {
            "engine": sys.executable,
            "engine_arguments": [str(ROOT / "tests/fake_engine.py"), "loop"],
            "position": "position startpos moves " + prefix.strip(),
            "nodes": 10,
            "threads": 1,
            "hash_mb": 16,
        }
        self.app.start(settings)
        self.wait_for_games(1)
        self.app.control("stop_after_games")
        self.app.child.wait(timeout=10)
        store = Store(Path(self.app.config["runtime_state_dir"]) / "state.sqlite3")
        try:
            game = json.loads(
                store.db.execute(
                    "SELECT payload FROM games WHERE json_extract(payload,'$.completed')=1 ORDER BY rowid LIMIT 1"
                ).fetchone()[0]
            )
            self.assertEqual(game["reason"], "repetition")
            self.assertEqual(game["rule_result"], "draw")
            self.assertEqual(game["research_result"], "white_win")
            self.assertEqual(game["repetition_history_start_ply"], 12)
            self.assertEqual(len(game["moves"]), 24)
        finally:
            store.close()

    def test_move_limit_learning_and_optional_search_details(self):
        for draw, details in (("b", False), ("w", True), ("ignore", False)):
            with self.subTest(draw=draw, details=details):
                settings = app.DEFAULTS | {
                    "engine": sys.executable,
                    "engine_arguments": [str(ROOT / "tests/fake_engine.py")],
                    "nodes": 10,
                    "threads": 1,
                    "hash_mb": 16,
                    "max_plies": 1,
                    "draw": draw,
                    "save_search_details": details,
                    "tasks": [{"label": "起点", "moves": []}],
                }
                self.app.start(settings)
                self.wait_for_games(1)
                self.app.control("stop_after_games")
                self.app.child.wait(timeout=10)
                runtime = Path(self.app.config["runtime_state_dir"])
                store = Store(runtime / "state.sqlite3")
                try:
                    game = json.loads(
                        store.db.execute(
                            "SELECT payload FROM games WHERE json_extract(payload,'$.completed')=1 ORDER BY rowid LIMIT 1"
                        ).fetchone()[0]
                    )
                    self.assertEqual(game["reason"], "max_total_plies")
                    self.assertEqual(game["rule_result"], "unresolved")
                    self.assertEqual(
                        game["research_result"],
                        {"b": "white_win", "w": "black_win", "ignore": "unresolved"}[
                            draw
                        ],
                    )
                    self.assertEqual(
                        store.service()["sequential_study"]["tasks"][0]["black_streak"],
                        0,
                    )
                    info = game["selections"][0]["info"]
                    self.assertIn("score", info)
                    self.assertEqual("pv" in info, details)
                    self.assertEqual("nodes" in info, details)
                    if draw == "ignore":
                        self.assertFalse(store.candidates())
                    else:
                        first_move = next(
                            r
                            for r in store.candidates().values()
                            if r["move"] == "7g7f"
                        )
                        self.assertEqual(first_move["active"], draw == "w")
                finally:
                    store.close()
                log = runtime / "workers/1/usi.jsonl"
                if details:
                    log_text = log.read_text(encoding="utf-8")
                    self.assertIn(" pv 7g7f", log_text)
                    sent_options = [
                        json.loads(line)["line"]
                        for line in log_text.splitlines()
                        if json.loads(line)["direction"] == "in"
                        and json.loads(line)["line"].startswith("setoption ")
                    ]
                    self.assertEqual(
                        sent_options,
                        [
                            "setoption name Threads value 1",
                            "setoption name USI_Hash value 16",
                            "setoption name ResignValue value 300",
                        ],
                    )
                else:
                    self.assertFalse(log.exists())

    def test_import_is_read_once_and_resume_does_not_need_original_book(self):
        book = Path(self.tmp.name) / "input.db"
        key = " ".join(app.cshogi.Board().sfen().split()[:3])
        book.write_text("sfen " + key + " 1\n7g7f none 0 32\n", encoding="utf-8")
        settings = app.DEFAULTS | {
            "engine": sys.executable,
            "book": str(book),
            "engine_arguments": [
                str(ROOT / "tests/fake_engine.py"),
                "game_delay",
                "0.03",
            ],
            "nodes": 10,
            "threads": 1,
            "hash_mb": 16,
        }
        with patch.object(app, "read_book", wraps=app.read_book) as read:
            self.app.start(settings)
            self.assertEqual(read.call_count, 1)
        self.wait_for_games(2)
        self.app.control("stop_after_games")
        self.app.child.wait(timeout=10)
        before = self.app.status()["completed_games"]
        book.unlink()
        self.app = app.Application(Path(self.tmp.name))
        with patch.object(
            app, "read_book", side_effect=AssertionError("reread import")
        ):
            self.app.start(settings)
        self.wait_for_games(before + 1)

    def test_public_etag_and_gzip(self):
        port = self.servers[1].server_port
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/tree", headers={"Accept-Encoding": "gzip"})
        response = connection.getresponse()
        import gzip

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.read()))["count"], 0)
        etag = response.getheader("ETag")
        connection.request("GET", "/api/tree", headers={"If-None-Match": etag})
        response = connection.getresponse()
        self.assertEqual(response.status, 304)
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_start_publish_stop_restart_and_download(self):
        settings = app.DEFAULTS | {
            "engine": sys.executable,
            "engine_arguments": [
                str(ROOT / "tests/fake_engine.py"),
                "game_delay",
                "0.03",
            ],
            "nodes": 10,
            "threads": 1,
            "hash_mb": 16,
            "parallel": 2,
        }
        code, data = self.request("/api/start", "POST", settings)
        self.assertEqual(code, 200, data)
        self.wait_for_games(4)
        state = json.loads(self.request("/api/state", public=True)[1])
        self.assertNotIn("engine", state)
        self.assertNotIn("runtime_state_dir", state)
        self.assertNotIn("token", state)
        self.assertTrue(state["completed_games"] >= 4)
        changed = settings | {"nodes": 11}
        self.assertEqual(self.request("/api/start", "POST", changed)[0], 400)
        tree = json.loads(self.request("/api/tree", public=True)[1])
        self.assertGreaterEqual(tree["count"], 4)
        self.assertIn(b"#YANEURAOU-DB2016", self.request("/book.db", public=True)[1])
        games = self.request("/games.sfen", public=True)[1].decode()
        self.assertGreaterEqual(len(games.splitlines()), 4)
        self.assertTrue(all(line.endswith("resign") for line in games.splitlines()))
        download = HTTPConnection("127.0.0.1", self.servers[1].server_port, timeout=5)
        try:
            download.request("GET", "/sfens.txt")
            response = download.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.getheader("Content-Disposition"),
                'attachment; filename="sfens.txt"',
            )
            self.assertTrue(response.read().startswith(games.encode("utf-8")))
        finally:
            download.close()
        self.request("/api/control", "POST", {"operation": "stop_after_games"})
        self.app.child.wait(timeout=10)
        before = self.app.status()["completed_games"]
        # Simulate the browser/control process restarting; the same data resumes.
        self.app = app.Application(Path(self.tmp.name))
        self.app.start(settings)
        self.wait_for_games(before + 2)
        store = Store(Path(self.app.config["runtime_state_dir"]) / "state.sqlite3")
        try:
            self.assertEqual(store.service()["learning_policy"], "legacy_backtrack")
        finally:
            store.close()

    def wait_for_games(self, count):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.app.status().get("completed_games", 0) >= count:
                return
            if self.app.child and self.app.child.poll() is not None:
                log = Path(self.app.config["runtime_state_dir"]).parent / "launch.log"
                self.fail(log.read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.05)
        runtime = Path(self.app.config["runtime_state_dir"])
        self.fail(
            (runtime / "manager.log").read_text(encoding="utf-8", errors="replace")[
                -6000:
            ]
        )


if __name__ == "__main__":
    unittest.main()
