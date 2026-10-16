"""Bounded Windows USI transport: stdout reader + queue + monotonic deadlines."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import queue
import subprocess
import threading
import time

from .runtime_log import SafeLog
from .process_job import ProcessJob


class EngineError(RuntimeError):
    pass


class EngineTimeout(EngineError):
    pass


class EngineAborted(EngineError):
    pass


class Engine:
    def __init__(
        self,
        command,
        cwd=None,
        options=None,
        log_path=None,
        init_timeout=120,
        ready_timeout=600,
        stop_grace=2,
        log_search_details=True,
    ):
        self.command = command
        self.cwd = cwd
        self.options = options or {}
        self.log_path = Path(log_path) if log_path else None
        self.init_timeout, self.ready_timeout = init_timeout, ready_timeout
        self.stop_grace = stop_grace
        self.process = None
        self.queue = None
        self.reader = None
        self.log_lock = threading.Lock()
        self.log_sink = SafeLog(self.log_path) if self.log_path else None
        self.job = None
        self.dropped_output = 0
        self.log_search_details = log_search_details

    @property
    def log_error(self):
        return self.log_sink.error if self.log_sink else None

    def log(self, direction, line):
        if not self.log_search_details and direction in ("in", "out"):
            return
        if self.log_sink:
            self.log_sink.write(
                json.dumps(
                    {"time": time.time(), "direction": direction, "line": line},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def start(self, aborted=None):
        self.close()
        output = self.queue = queue.Queue(maxsize=4096)
        self.job = ProcessJob()
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            self.job.close()
            self.job = None
            raise
        process = self.process
        try:
            self.job.attach(process)
        except OSError:
            process.kill()
            process.wait(timeout=5)
            self.close()
            raise

        def enqueue(line):
            try:
                output.put_nowait(line)
            except queue.Full:
                self.dropped_output += 1
                if line is None or not line.startswith("info "):
                    try:
                        output.get_nowait()
                    except queue.Empty:
                        pass
                    output.put_nowait(line)

        def read_output():
            try:
                while True:
                    line = process.stdout.readline(65536)
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    self.log("out", line)
                    enqueue(line)
            finally:
                enqueue(None)

        self.reader = threading.Thread(target=read_output, daemon=True)
        self.reader.start()
        try:
            self.send("usi")
            lines = self.wait("usiok", self.init_timeout, aborted)
            supported = set()
            for line in lines:
                if line.startswith("option name ") and " type " in line:
                    supported.add(line[12:].split(" type ", 1)[0])
            options = dict(self.options)
            if "BookFile" in supported:
                options["BookFile"] = "no_book"
            elif "OwnBook" in supported:
                options["OwnBook"] = False
            if "EnteringKingRule" not in supported:
                options.pop("EnteringKingRule", None)
            for name in ("Threads", "ResignValue"):
                if name in options and name not in supported:
                    options.pop(name)
                    self.log(
                        "warning",
                        f"{name} is not advertised; engine default will be used",
                    )
            # USI_Hash is a reserved GUI option; some engines omit its declaration.
            unknown = options.keys() - supported - {"USI_Hash"}
            if unknown:
                raise EngineError(f"Unsupported USI options: {sorted(unknown)}")
            for name, value in options.items():
                text = str(value).lower() if isinstance(value, bool) else str(value)
                if "\n" in text or "\r" in text or "\n" in name or "\r" in name:
                    raise EngineError("Newlines in USI options")
                self.send(f"setoption name {name} value {text}")
            self.send("isready")
            self.wait("readyok", self.ready_timeout, aborted)
        except BaseException:
            self.close()
            raise

    def send(self, line):
        if not self.process or self.process.poll() is not None:
            raise EngineError("Engine exited")
        try:
            self.log("in", line)
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise EngineError("Engine input closed") from error

    def wait(self, token, seconds, aborted=None):
        deadline, lines = time.monotonic() + seconds, deque(maxlen=2048)
        while True:
            if aborted and aborted():
                raise EngineAborted("Immediate abort requested")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineTimeout(f"No {token} within {seconds}s")
            try:
                line = self.queue.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if line is None:
                raise EngineError(f"EOF while waiting for {token}")
            lines.append(line)
            if line.split(" ", 1)[0] == token:
                return list(lines)

    def search(self, position, nodes, seconds, aborted=None):
        self.send(position)
        self.send(f"go nodes {nodes}")
        try:
            lines = self.wait("bestmove", seconds, aborted)
        except (EngineTimeout, EngineAborted) as error:
            try:
                self.send("stop")
                self.wait("bestmove", self.stop_grace)
            except EngineError:
                pass
            # A late response never changes an interrupted game to a result.
            self.close()
            raise error
        best = lines[-1].split()
        if len(best) < 2:
            raise EngineError("Malformed bestmove")
        info = {}
        for line in lines:
            fields = line.split()
            if not fields or fields[0] != "info":
                continue
            for field in ("depth", "nodes", "time", "nps", "hashfull"):
                if field in fields:
                    index = fields.index(field)
                    if index + 1 < len(fields):
                        try:
                            info[field] = int(fields[index + 1])
                        except ValueError:
                            pass
            if "score" in fields:
                index = fields.index("score")
                if index + 2 < len(fields):
                    info["score"] = {
                        "kind": fields[index + 1],
                        "value": fields[index + 2],
                    }
            if "pv" in fields:
                info["pv"] = fields[fields.index("pv") + 1 :]
        return best[1], info

    def close(self):
        process = self.process
        if process is None:
            if self.job:
                self.job.close()
                self.job = None
            return
        if process.poll() is None:
            try:
                self.send("quit")
                process.wait(timeout=self.stop_grace)
            except (EngineError, OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Closing the kernel job below also kills descendants.
        if self.job:
            self.job.close()
            self.job = None
        if self.reader:
            self.reader.join(timeout=2)
        for handle in (process.stdin, process.stdout):
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass  # A closed Windows pipe may fail while flushing stdin.
        self.process = None
