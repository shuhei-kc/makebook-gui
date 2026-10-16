"""Browser control on localhost; a separate read-only spectator for publication."""

from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
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
from core.publication import Publications
from core.runner import load_config, position_board
from core.service import read_published_status, merge_health, serve_campaign
from core.store import ManagerLock, Store, atomic_write
from core.tasks import submit

ROOT = Path(__file__).resolve().parent
DEFAULTS = dict(
    engine="",
    engine_arguments=[],
    position="position startpos",
    nodes=20000000,
    threads=4,
    hash_mb=1024,
    parallel=1,
    resign=300,
    draw="ignore",
    max_plies=256,
    book="",
    fixed_side="",
)


def checked_settings(raw):
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        raise ValueError("設定の項目を確認してください")
    s = DEFAULTS | raw
    if any(not isinstance(s[k], str) for k in ("engine", "position", "book")):
        raise ValueError("エンジン・開始局面・定跡パスは文字列で指定してください")
    for name, low, high in [
        ("nodes", 1, 10**12),
        ("threads", 1, 1024),
        ("hash_mb", 1, 1048576),
        ("parallel", 1, 8),
        ("resign", 0, 99999),
        ("max_plies", 1, 10000),
    ]:
        if type(s[name]) is not int or not low <= s[name] <= high:
            raise ValueError(f"{name}: {low}〜{high}の整数を指定してください")
    if s["draw"] not in ("ignore", "b", "w") or s["fixed_side"] not in ("", "b", "w"):
        raise ValueError("千日手・固定する側の指定を確認してください")
    if s["fixed_side"] and not s["book"]:
        raise ValueError("固定する場合は読み込み定跡DBを指定してください")
    if not isinstance(s["engine_arguments"], list) or not all(
        isinstance(x, str) for x in s["engine_arguments"]
    ):
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
        s["book"] = str(book.resolve())
    return s


def load_book(settings):
    book = read_book(settings["book"]) if settings["book"] else {}
    for key, moves in book.items():
        try:
            board = cshogi.Board(key + " 1")
            if (
                not board.is_ok()
                or key_of(board) != key
                or any(not board.is_legal(board.move_from_usi(u)) for u in moves)
            ):
                raise ValueError("Illegal move")
        except (ValueError, RuntimeError) as error:
            raise ValueError(
                f"読み込み定跡の局面・指し手を確認してください: {key}"
            ) from error
    return book


def campaign(settings, directory, prepared_book=None):
    s = settings
    fixed = []
    if s["book"] and s["fixed_side"]:
        book = prepared_book if prepared_book is not None else load_book(s)
        fixed = [
            {"canonical_sfen": key, "moves": [{"usi": u} for u in moves]}
            for key, moves in book.items()
            if key.split()[1] == s["fixed_side"]
        ]
    return dict(
        single_worker_version=1,
        campaign_id=directory.name,
        runtime_state_dir=str(directory / "runtime"),
        queue_dir=str(directory / "tasks"),
        result_dir=str(directory / "results"),
        start_paused=False,
        workers=s["parallel"],
        start_position=s["position"],
        fixed_rules=fixed,
        initial_book=s["book"],
        initial_book_fixed=bool(s["fixed_side"]),
        max_total_plies=s["max_plies"],
        repetition_limit=4,
        research_outcomes={
            "repetition": {"ignore": "draw", "b": "white_win", "w": "black_win"}[
                s["draw"]
            ],
            "max_total_plies": "unresolved",
        },
        sfen_archive=str(directory / "games.sfen"),
        engine=dict(
            executable=s["engine"],
            arguments=s["engine_arguments"],
            cwd=str(Path(s["engine"]).parent),
            nodes=s["nodes"],
            usi_options={
                "Threads": s["threads"],
                "USI_Hash": s["hash_mb"],
                "ResignValue": s["resign"],
            },
        ),
    )


def initialize_book(config, prepared_book=None):
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
                    imported = (
                        prepared_book
                        if prepared_book is not None
                        else read_book(config["initial_book"])
                    )
                    for key, moves in imported.items():
                        book.setdefault(key, {}).update({u: 1 for u in moves})
            _, origin, moves, *_ = position_board(config["start_position"])
            b = (
                cshogi.Board()
                if origin == "startpos"
                else cshogi.Board(origin.removeprefix("sfen "))
            )
            for u in moves:
                book[key_of(b)] = {u: 1000}
                b.push_usi(u)
            candidates = Candidates()
            for key, moves in book.items():
                for u, count in moves.items():
                    row = candidates.register(key, u)
                    row["wins"] = count
            store.commit(
                "campaign_created",
                {"config": config, "upserts": candidates.state},
                "campaign:" + config["campaign_id"],
            )
        finally:
            store.close()


class Application:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.start_lock = threading.Lock()
        self.publications = Publications()
        self.token = secrets.token_urlsafe(32)
        self.child = None
        self.current = self.data / "current.json"
        self.config = None
        self.config_path = None
        self.startup_warning = ""
        self.settings = deepcopy(DEFAULTS)
        if self.current.exists():
            try:
                saved = json.loads(self.current.read_text(encoding="utf-8"))
                config_path = Path(saved["config"])
                # Viewing saved games does not require an installed engine.
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(saved["settings"], dict) or not all(
                    isinstance(config[k], str)
                    for k in ("campaign_id", "runtime_state_dir", "start_position")
                ):
                    raise ValueError("Invalid saved session")
                self.settings = deepcopy(DEFAULTS) | saved["settings"]
                self.config, self.config_path = config, config_path
            except (OSError, ValueError, KeyError, TypeError):
                self.startup_warning = "前回の設定を読み込めませんでした。保存データを残して未開始で起動しました。"
                logging.warning(self.startup_warning)

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
        with self.start_lock:
            settings = checked_settings(raw)
            with self.guard:
                if self.running():
                    if settings != self.settings:
                        raise ValueError(
                            "現在局の終了後に停止してから設定を変更してください"
                        )
                    return self.control("resume")
                new_session = self.config is None or settings != self.settings
                cfg, config = self.config_path, self.config
            if new_session:
                # Parse/validate once, outside the control lock. Initialize the
                # journal from this exact snapshot before starting the worker.
                imported = load_book(settings)
                directory = self.data / (
                    "study-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
                )
                directory.mkdir()
                cfg = directory / "campaign.json"
                atomic_write(
                    cfg,
                    json.dumps(
                        campaign(settings, directory, imported),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                config = load_config(cfg)
                initialize_book(config, imported)
            with self.guard:
                if new_session:
                    atomic_write(
                        self.current,
                        json.dumps(
                            {"settings": settings, "config": str(cfg)},
                            ensure_ascii=False,
                        ),
                    )
                    self.settings, self.config, self.config_path = settings, config, cfg
                    self.startup_warning = ""
                else:
                    self.control("resume")
                log_path = Path(config["runtime_state_dir"]).parent / "launch.log"
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
                with log_path.open("ab") as log:
                    self.child = subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            str(ROOT / "app.py"),
                            "--worker",
                            str(cfg),
                        ],
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        creationflags=flags,
                        start_new_session=os.name != "nt",
                    )
                return {"state": "starting", "session": config["campaign_id"]}

    def control(self, operation):
        with self.guard:
            if not isinstance(operation, str):
                raise ValueError("operationは文字列で指定してください")
            if not self.config or operation not in {
                "resume",
                "pause",
                "stop_after_games",
            }:
                raise ValueError("対局を開始してください")
            task_id = "web-" + uuid.uuid4().hex
            submit(
                self.config,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "kind": "control",
                    "target": {"campaign_id": self.config["campaign_id"]},
                    "operation": operation,
                },
            )
            return {"state": "submitted", "task_id": task_id}

    def status(self):
        with self.guard:
            if not self.config:
                return {"phase": "not_started", "completed_games": 0, "games": []}
            runtime = Path(self.config["runtime_state_dir"])
            try:
                raw = merge_health(
                    runtime, read_published_status(runtime, self.config["campaign_id"])
                )
            except FileNotFoundError:
                return {
                    "phase": "starting" if self.running() else "stopped",
                    "completed_games": 0,
                    "games": [],
                }
            result = {
                k: raw.get(k)
                for k in (
                    "phase",
                    "completed_games",
                    "results",
                    "updated_unix",
                    "active_workers",
                )
            }
            result["session"] = self.config["campaign_id"]
            result["stale"] = (
                raw.get("phase") != "stopped"
                and time.time() - raw.get("updated_unix", 0) > 10
            )
            result["games"] = []
            for checkpoint in sorted((runtime / "workers").glob("*/in_progress.json")):
                try:
                    game = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if game.get("campaign_id") != self.config["campaign_id"]:
                        continue
                    b = (
                        cshogi.Board()
                        if game["origin"] == "startpos"
                        else cshogi.Board(game["origin"].removeprefix("sfen "))
                    )
                    for u in game["moves"]:
                        b.push_usi(u)
                    result["games"].append(
                        dict(
                            worker=game.get("worker_id", int(checkpoint.parent.name)),
                            sfen=b.sfen(),
                            moves=game["moves"],
                        )
                    )
                except (OSError, ValueError, KeyError):
                    continue
            return result

    def publication(self, kind):
        with self.guard:
            config = self.config
        return self.publications.read(config, kind)

    def tree_data(self):
        return json.loads(self.publication("tree").body)

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
        return self.publication(kind).body.decode("utf-8")


def handler_for(app, admin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(
            self,
            body,
            status=200,
            content_type="application/json; charset=utf-8",
            attachment=None,
            etag=None,
            compressed=False,
        ):
            if not isinstance(body, bytes):
                body = (
                    json.dumps(body, ensure_ascii=False).encode()
                    if not isinstance(body, str)
                    else body.encode()
                )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if status != 304:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
                self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            )
            if attachment:
                self.send_header(
                    "Content-Disposition", 'attachment; filename="' + attachment + '"'
                )
            self.end_headers()
            self.wfile.write(body)

        def published(self, kind, attachment=None):
            response = app.publication(kind)
            content_type = (
                "application/json; charset=utf-8"
                if kind == "tree"
                else "text/plain; charset=utf-8"
            )
            if self.headers.get("If-None-Match") == response.etag:
                return self.send(b"", 304, content_type, etag=response.etag)
            encodings = self.headers.get("Accept-Encoding", "").lower().split(",")
            compressed = any(value.strip() == "gzip" for value in encodings)
            return self.send(
                response.compressed if compressed else response.body,
                content_type=content_type,
                attachment=attachment,
                etag=response.etag,
                compressed=compressed,
            )

        def local_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def do_GET(self):
            if admin and not self.local_host():
                return self.send({"error": "Invalid host"}, 403)
            url = urlsplit(self.path)
            try:
                if url.path == "/api/settings" and admin:
                    return self.send(
                        {
                            "settings": app.settings,
                            "token": app.token,
                            "running": app.running(),
                            "warning": app.startup_warning,
                            "public_url": f"http://127.0.0.1:{getattr(app, 'public_port', 8766)}/",
                        }
                    )
                if url.path == "/api/state":
                    return self.send(app.status())
                if url.path == "/api/tree":
                    return self.published("tree")
                if url.path == "/api/position":
                    return self.send(
                        {
                            "sfen": app.position(
                                parse_qs(url.query).get("moves", [""])[0]
                            )
                        }
                    )
                if url.path in ("/book.db", "/games.sfen"):
                    name = url.path[1:]
                    return self.published(name, attachment=name)
                files = {
                    "/": "manage.html" if admin else "index.html",
                    "/app.js": "app.js",
                    "/app.css": "app.css",
                }
                if url.path not in files:
                    return self.send({"error": "Not found"}, 404)
                name = files[url.path]
                mime = {
                    ".html": "text/html",
                    ".js": "text/javascript",
                    ".css": "text/css",
                }[Path(name).suffix]
                return self.send(
                    (ROOT / "web" / name).read_bytes(),
                    content_type=mime + "; charset=utf-8",
                )
            except ValueError as error:
                if url.path == "/api/position":
                    return self.send({"error": str(error)}, 400)
                logging.exception("Invalid publication data")
                return self.send(
                    {"error": "公開データを読み込めません。ログを確認してください"}, 500
                )
            except (OSError, sqlite3.Error):
                return self.send({"error": "データ更新待ちです"}, 503)
            except Exception:
                logging.exception("GET request failed")
                return self.send(
                    {"error": "処理に失敗しました。ログを確認してください"}, 500
                )

        def do_POST(self):
            if not admin:
                return self.send({"error": "Read only"}, 405)
            origin = self.headers.get("Origin")
            if (
                not self.local_host()
                or origin != "http://" + self.headers.get("Host", "")
                or not secrets.compare_digest(
                    self.headers.get("X-Makebook-Token", ""), app.token
                )
            ):
                return self.send({"error": "Invalid origin or token"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("リクエストが大きすぎます")
                raw = json.loads(self.rfile.read(size))
                if not isinstance(raw, dict):
                    raise ValueError("JSONオブジェクトで指定してください")
                if self.path == "/api/start":
                    return self.send(app.start(raw))
                if self.path == "/api/control":
                    return self.send(app.control(raw.get("operation")))
                return self.send({"error": "Not found"}, 404)
            except (ValueError, OSError) as error:
                return self.send({"error": str(error)}, 400)
            except Exception:
                logging.exception("POST request failed")
                return self.send(
                    {"error": "処理に失敗しました。ログを確認してください"}, 500
                )

    return Handler


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="連続対局とWeb公開")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--port", type=int, default=8765, help="このPCの操作画面")
    parser.add_argument(
        "--public-port", type=int, default=8766, help="読み取り専用の観戦画面"
    )
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
    with ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler_for(app, True)
    ) as manager:
        with ThreadingHTTPServer(
            ("127.0.0.1", args.public_port), handler_for(app, False)
        ) as viewer:
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
