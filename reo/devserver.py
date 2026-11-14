"""Run the UI in a normal browser against the real Bridge (no pywebview needed).

    python -m reo.devserver            # http://127.0.0.1:8765
    REO_MOCK_ENGINE=1 python -m reo.devserver   # fake model, instant

POST /api/<method>  {"args": [...]}  -> JSON result of Bridge.<method>(*args)
GET  /events                         -> server-sent events carrying Bridge.emit() payloads
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .bridge import Bridge
from .config import resource_dir

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".png": "image/png",
        ".svg": "image/svg+xml", ".json": "application/json"}


def make_server(bridge_factory=Bridge, port: int = 8765):
    ui = resource_dir() / "ui"
    clients: list[queue.Queue] = []
    lock = threading.Lock()

    def emit(ev: dict):
        with lock:
            for q in clients:
                q.put(ev)

    bridge = bridge_factory(emit=emit)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/events":
                q: queue.Queue = queue.Queue()
                with lock:
                    clients.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with lock:
                        if q in clients:
                            clients.remove(q)
                return
            rel = self.path.split("?")[0].lstrip("/") or "index.html"
            p = (ui / rel).resolve()
            if ui.resolve() not in p.parents and p != ui.resolve() or not p.is_file():
                return self._send(404, b"not found", "text/plain")
            data = p.read_bytes()
            if p.name == "index.html":
                data = data.replace(b"<!--DEV-->", b'<script src="/devshim.js"></script>')
            self._send(200, data, MIME.get(p.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self.path.startswith("/api/"):
                return self._send(404, b"{}")
            name = self.path[5:]
            if name.startswith("_") or not hasattr(bridge, name):
                return self._send(404, b'{"error":"no such method"}')
            n = int(self.headers.get("Content-Length", 0))
            args = json.loads(self.rfile.read(n) or b"{}").get("args", [])
            try:
                out = getattr(bridge, name)(*args)
                self._send(200, json.dumps(out).encode())
            except Exception as e:      # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv, bridge


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv, _ = make_server(port=port)
    print(f"Reo dev UI on http://127.0.0.1:{port}")
    srv.serve_forever()
