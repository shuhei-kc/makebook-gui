"""Browser control on localhost; a separate read-only spectator for publication."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

import cshogi

from core.learning import Candidates, key_of, read_book
from core.runner import load_config, position_board
from core.service import read_published_status, merge_health, serve_campaign
from core.store import ManagerLock, Store, atomic_write
from core.tasks import submit

ROOT = Path(__file__).resolve().parent
DEFAULTS = dict(engine="", engine_arguments=[], position="position startpos",
                nodes=20000000, threads=4, hash_mb=1024, parallel=1,
                resign=300, draw="ignore", max_plies=256, book="", fixed_side="")


def checked_settings(raw):
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        raise ValueError("設定の項目を確認してください")
    s = DEFAULTS | raw
    if any(not isinstance(s[k], str) for k in ("engine", "position", "book")):
        raise ValueError("エンジン・開始局面・定跡パスは文字列で指定してください")
    for name, low, high in [("nodes", 1, 10**12), ("threads", 1, 1024),
                            ("hash_mb", 1, 1048576), ("parallel", 1, 8),
                            ("resign", 0, 99999), ("max_plies", 1, 10000)]:
        if type(s[name]) is not int or not low <= s[name] <= high:
            raise ValueError(f"{name}: {low}〜{high}の整数を指定してください")
    if s["draw"] not in ("ignore", "b", "w") or s["fixed_side"] not in ("", "b", "w"):
        raise ValueError("千日手・固定する側の指定を確認してください")
    if s["fixed_side"] and not s["book"]:
        raise ValueError("固定する場合は読み込み定跡DBを指定してください")
    if not isinstance(s["engine_arguments"], list) or not all(isinstance(x, str) for x in s["engine_arguments"]):
        raise ValueError("engine_argumentsは文字列の配列です")
    engine = Path(s["engine"]).expanduser()
    engine = engine if engine.is_absolute() else ROOT / engine
    if not engine.is_file():
        raise ValueError("USIエンジンの実行ファイルを指定してください")
    s["engine"] = str(engine.resolve())
    board, *_ = position_board(s["position"])
    if s["max_plies"] <= board.move_number - 1:
        raise ValueError("手数上限は開始局面の手数より大きくしてください")
    if s["book"]:
        book = Path(s["book"]).expanduser()
        book = book if book.is_absolute() else ROOT / book
        for key, moves in read_book(book).items():
            b = cshogi.Board(key + " 1")
            if any(not b.is_legal(b.move_from_usi(u)) for u in moves):
                raise ValueError("読み込み定跡に非合法手があります")
        s["book"] = str(book.resolve())
    return s


def campaign(settings, directory):
    s = settings
    fixed = []
    if s["book"] and s["fixed_side"]:
        fixed = [{"canonical_sfen": key, "moves": [{"usi": u} for u in moves]}
                 for key, moves in read_book(s["book"]).items() if key.split()[1] == s["fixed_side"]]
    return dict(single_worker_version=1, campaign_id=directory.name,
                runtime_state_dir=str(directory / "runtime"),
                queue_dir=str(directory / "tasks"), result_dir=str(directory / "results"),
                start_paused=False, workers=s["parallel"], start_position=s["position"],
                fixed_rules=fixed, initial_book=s["book"], initial_book_fixed=bool(s["fixed_side"]),
                max_total_plies=s["max_plies"], repetition_limit=4,
                research_outcomes={"repetition": {"ignore": "draw", "b": "white_win", "w": "black_win"}[s["draw"]],
                                   "max_total_plies": "unresolved"},
                sfen_archive=str(directory / "games.sfen"),
                engine=dict(executable=s["engine"], arguments=s["engine_arguments"],
                            cwd=str(Path(s["engine"]).parent), nodes=s["nodes"],
                            usi_options={"Threads": s["threads"], "USI_Hash": s["hash_mb"], "ResignValue": s["resign"]}))


def initialize_book(config):
    """Import at one, then overwrite start-route positions at 1000, as upstream."""
    runtime = Path(config["runtime_state_dir"])
    with ManagerLock(runtime):
        store = Store(runtime / "state.sqlite3")
        try:
            if store.config() is not None:
                return
            book = {}
            if not config.get("initial_book_fixed"):
                if config.get("initial_book"):
                    for key, moves in read_book(config["initial_book"]).items():
                        book.setdefault(key, {}).update({u: 1 for u in moves})
            _, origin, moves, *_ = position_board(config["start_position"])
            b = cshogi.Board() if origin == "startpos" else cshogi.Board(origin.removeprefix("sfen "))
            for u in moves:
                book[key_of(b)] = {u: 1000}
                b.push_usi(u)
            candidates = Candidates()
            for key, moves in book.items():
                for u, count in moves.items():
                    row = candidates.register(key, u)
                    row["wins"] = count
            store.commit("campaign_created", {"config": config, "upserts": candidates.state},
                         "campaign:" + config["campaign_id"])
        finally:
            store.close()


class Application:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.child = None
        self.current = self.data / "current.json"
        self.config = None
        self.settings = deepcopy(DEFAULTS)
        if self.current.exists():
            saved = json.loads(self.current.read_text(encoding="utf-8"))
            self.settings = saved["settings"]
            # Viewing saved games must not require the engine to remain installed.
            self.config = json.loads(Path(saved["config"]).read_text(encoding="utf-8"))
        self.tree_session = None
        self.tree_seq = None
        self.last_row = 0
        self.tree = None

    def running(self):
        if self.child is not None and self.child.poll() is None:
            return True
        if not self.config:
            return False
        try:
            with ManagerLock(self.config["runtime_state_dir"]):
                return False
        except RuntimeError:
            return True

    def start(self, raw):
        with self.guard:
            settings = checked_settings(raw)
            if self.running():
                if settings != self.settings:
                    raise ValueError("現在局の終了後に停止してから設定を変更してください")
                return self.control("resume")
            if self.config is None or settings != self.settings:
                directory = self.data / ("study-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
                directory.mkdir()
                cfg = directory / "campaign.json"
                atomic_write(cfg, json.dumps(campaign(settings, directory), ensure_ascii=False, indent=2))
                config = load_config(cfg)
                atomic_write(self.current, json.dumps({"settings": settings, "config": str(cfg)}, ensure_ascii=False))
                self.settings, self.config = settings, config
            else:
                cfg = Path(json.loads(self.current.read_text(encoding="utf-8"))["config"])
                # A previous intentional stop remains paused in the journal.
                self.control("resume")
            log_path = Path(self.config["runtime_state_dir"]).parent / "launch.log"
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                     getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            with log_path.open("ab") as log:
                self.child = subprocess.Popen([sys.executable, "-B", str(ROOT / "app.py"), "--worker", str(cfg)],
                                              cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                              creationflags=flags, start_new_session=os.name != "nt")
            return {"state": "starting", "session": self.config["campaign_id"]}

    def control(self, operation):
        if not self.config or operation not in {"resume", "pause", "stop_after_games"}:
            raise ValueError("対局を開始してください")
        task_id = "web-" + uuid.uuid4().hex
        submit(self.config, {"schema_version": 1, "task_id": task_id, "kind": "control",
                            "target": {"campaign_id": self.config["campaign_id"]}, "operation": operation})
        return {"state": "submitted", "task_id": task_id}

    def status(self):
        with self.guard:
            if not self.config:
                return {"phase": "not_started", "completed_games": 0, "games": []}
            runtime = Path(self.config["runtime_state_dir"])
            try:
                raw = merge_health(runtime, read_published_status(runtime, self.config["campaign_id"]))
            except FileNotFoundError:
                return {"phase": "starting" if self.running() else "stopped", "completed_games": 0, "games": []}
            result = {k: raw.get(k) for k in ("phase", "completed_games", "results", "updated_unix", "active_workers")}
            result["stale"] = raw.get("phase") != "stopped" and time.time() - raw.get("updated_unix", 0) > 10
            result["games"] = []
            for checkpoint in sorted((runtime / "workers").glob("*/in_progress.json")):
                try:
                    game = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if game.get("campaign_id") != self.config["campaign_id"]:
                        continue
                    b = cshogi.Board() if game["origin"] == "startpos" else cshogi.Board(game["origin"].removeprefix("sfen "))
                    for u in game["moves"]:
                        b.push_usi(u)
                    result["games"].append(dict(worker=game.get("worker_id", int(checkpoint.parent.name)),
                                                sfen=b.sfen(), moves=game["moves"]))
                except (OSError, ValueError, KeyError):
                    continue
            return result

    def connect(self):
        if not self.config:
            return None
        db = Path(self.config["runtime_state_dir"]) / "state.sqlite3"
        if not db.exists():
            return None
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=.5)
        conn.execute("PRAGMA query_only=ON")
        return conn

    def tree_data(self):
        with self.guard:
            conn = self.connect()
            if conn is None:
                return {"move": "開始局面", "count": 0, "children": {}}
            try:
                seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                if self.tree_session != self.config["campaign_id"]:
                    self.tree = {"move": "開始局面", "count": 0, "children": {}}
                    self.last_row = 0
                    self.tree_seq = None
                    self.tree_session = self.config["campaign_id"]
                if seq != self.tree_seq:
                    prefix = len(position_board(self.config["start_position"])[2])
                    rows = conn.execute("SELECT rowid,payload FROM games WHERE rowid>? ORDER BY rowid", (self.last_row,)).fetchall()
                    for row_id, payload in rows:
                        game = json.loads(payload)
                        self.last_row = row_id
                        if not game.get("completed"):
                            continue
                        node = self.tree
                        node["count"] += 1
                        for u in game["moves"][prefix:]:
                            node = node["children"].setdefault(u, {"move": u, "count": 0, "children": {}})
                            node["count"] += 1
                    self.tree_seq = seq
                # Flat rows avoid a recursion limit for long games and make the
                # browser build only the expanded part of the tree.
                flat, stack = [], [(self.tree, -1)]
                while stack:
                    node, parent = stack.pop()
                    identity = len(flat)
                    flat.append(dict(id=identity, parent=parent, move=node["move"], count=node["count"]))
                    stack.extend((child, identity) for child in reversed(list(node["children"].values())))
                return {"count": self.tree["count"], "nodes": flat}
            finally:
                conn.close()

    def position(self, moves):
        if not self.config:
            return cshogi.Board().sfen()
        b, *_ = position_board(self.config["start_position"])
        words = moves.split()
        if len(words) > 10000:
            raise ValueError("手順が長すぎます")
        for u in words:
            m = b.move_from_usi(u)
            if not b.is_legal(m):
                raise ValueError("非合法手")
            b.push(m)
        return b.sfen()

    def download(self, kind):
        from core.exports import project
        conn = self.connect()
        if conn is None:
            return "#YANEURAOU-DB2016 1.00\n" if kind == "book.db" else ""
        try:
            conn.execute("BEGIN")
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            games = [json.loads(x[0]) for x in conn.execute("SELECT payload FROM games ORDER BY rowid")]
            candidates = [json.loads(x[0]) for x in conn.execute("SELECT payload FROM candidates")]
        finally:
            conn.close()
        full, _, book, _ = project(games, candidates, self.config["campaign_id"], seq)
        return book if kind == "book.db" else full


def handler_for(app, admin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, status=200, content_type="application/json; charset=utf-8", attachment=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, str) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            self.end_headers()
            self.wfile.write(body)

        def local_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if admin and not self.local_host():
                return self.send({"error": "Invalid host"}, 403)
            url = urlsplit(self.path)
            try:
                if url.path == "/api/settings" and admin:
                    return self.send({"settings": app.settings, "token": app.token, "running": app.running(),
                                      "public_url": f"http://127.0.0.1:{getattr(app, 'public_port', 8766)}/"})
                if url.path == "/api/state":
                    return self.send(app.status())
                if url.path == "/api/tree":
                    return self.send(app.tree_data())
                if url.path == "/api/position":
                    return self.send({"sfen": app.position(parse_qs(url.query).get("moves", [""])[0])})
                if url.path in ("/book.db", "/games.sfen"):
                    name = url.path[1:]
                    return self.send(app.download(name), content_type="text/plain; charset=utf-8", attachment=name)
                files = {"/": "manage.html" if admin else "index.html", "/app.js": "app.js", "/app.css": "app.css"}
                if url.path not in files:
                    return self.send({"error": "Not found"}, 404)
                name = files[url.path]
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[Path(name).suffix]
                return self.send((ROOT / "web" / name).read_bytes(), content_type=mime + "; charset=utf-8")
            except (OSError, ValueError, sqlite3.Error):
                return self.send({"error": "データ更新待ちです"}, 503)

        def do_POST(self):
            if not admin:
                return self.send({"error": "Read only"}, 405)
            origin = self.headers.get("Origin")
            if (not self.local_host() or origin != "http://" + self.headers.get("Host", "") or
                    not secrets.compare_digest(self.headers.get("X-Makebook-Token", ""), app.token)):
                return self.send({"error": "Invalid origin or token"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("リクエストが大きすぎます")
                raw = json.loads(self.rfile.read(size))
                if self.path == "/api/start":
                    return self.send(app.start(raw))
                if self.path == "/api/control":
                    return self.send(app.control(raw.get("operation")))
                return self.send({"error": "Not found"}, 404)
            except (ValueError, OSError) as error:
                return self.send({"error": str(error)}, 400)
    return Handler


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="連続対局とWeb公開")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--port", type=int, default=8765, help="このPCの操作画面")
    parser.add_argument("--public-port", type=int, default=8766, help="読み取り専用の観戦画面")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        config = load_config(args.worker)
        initialize_book(config)
        return serve_campaign(config, None, own_process=True)
    if args.port == args.public_port:
        parser.error("操作画面と観戦画面には別のポートを指定してください")
    app = Application(args.data)
    app.public_port = args.public_port
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(app, True)) as manager:
        with ThreadingHTTPServer(("127.0.0.1", args.public_port), handler_for(app, False)) as viewer:
            manager.daemon_threads = viewer.daemon_threads = True
            threading.Thread(target=viewer.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{args.port}/"
            print("操作画面: " + url, flush=True)
            print(f"公開用観戦画面: http://127.0.0.1:{args.public_port}/", flush=True)
            if not args.no_open:
                webbrowser.open(url)
            try:
                manager.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                viewer.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
