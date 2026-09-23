"""Local fixture server for the replicated browser benchmark (see BENCHMARK.md).

Serves goal-fixtures/bench over http://127.0.0.1:8777 and keeps each task's
page state in memory. Pages report what the user changed with `report({...})`
(bench.js); the evaluator reads it back with GET /state/<task>. Checks
therefore come from the page itself, never from Jev's done estimate.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "goal-fixtures" / "bench"
PORT = 8777
STATE: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, value: Any) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/state/"):
            with LOCK:
                self._json(STATE.get(self.path[len("/state/"):], {}))
            return
        super().do_GET()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/state/"):
            with LOCK:
                STATE.setdefault(self.path[len("/state/"):], {}).update(payload)
            self._json({"ok": True})
        elif self.path.startswith("/reset/"):
            with LOCK:
                STATE.pop(self.path[len("/reset/"):], None)
            self._json({"ok": True})
        else:
            self.send_error(404)


_server: ThreadingHTTPServer | None = None


def start() -> str:
    """Start once per process; returns the base URL."""
    global _server
    if _server is None:
        _server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT)))
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{PORT}"


def state(task: str) -> dict[str, Any]:
    with LOCK:
        return dict(STATE.get(task, {}))


def reset(task: str) -> None:
    with LOCK:
        STATE.pop(task, None)


if __name__ == "__main__":
    print(start())
    threading.Event().wait()
