#!/usr/bin/env python
"""project-hub - one local page listing every project service, with
start/stop/restart controls. Stdlib only, loopback only.

    python hub.py               http://127.0.0.1:8760
    python hub.py --port 9000 --no-browser

How it works:

- Status is a TCP connect to 127.0.0.1:<port> plus one `netstat -ano` pass to
  name the listening PID(s). Services are identified by their port - the hub
  never trusts PIDs it spawned earlier, because services outlive the hub.
- Stop kills the listener PIDs found on the port, but only if their image is
  on KILLABLE_IMAGES - anything else holding a managed port is reported as
  foreign and left alone.
- Start checks the port first: Windows lets two Pythons share one port
  (SO_REUSEADDR), so "already answers" must mean "do not spawn again".
- Services are spawned windowless (CREATE_NO_WINDOW) with stdout/stderr
  appended to logs/<id>.log; the UI's Log button tails those files. A
  service that keeps its own log (VocalCoach) is pointed at via services.json.
- POST endpoints require the X-Hub-Request header so a random browser page
  cannot drive them cross-origin.
"""
import argparse
import csv
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT.parent
REGISTRY_PATH = ROOT / "services.json"
INDEX_HTML = ROOT / "web" / "index.html"
LOG_DIR = ROOT / "logs"

PORT_DEFAULT = 8760
START_GRACE_SECONDS = 90  # disk-cleanup warms its cache for ~20 s
STOP_WAIT_SECONDS = 10
LOG_TAIL_MAX = 1000
LOG_TAIL_BYTES = 512 * 1024
LOG_ROTATE_BYTES = 4 * 1024 * 1024

KILLABLE_IMAGES = {
    "python.exe", "pythonw.exe", "python3.exe", "py.exe",
    "node.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
}

# When the hub runs under pythonw it owns no console, so every child console
# app (netstat, tasklist, taskkill - not just the services) gets a fresh,
# VISIBLE window unless this flag is passed. That was the terminal-flicker
# storm: one window per subprocess per status poll.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def port_open(port, timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def listeners_by_port():
    """port -> set of PIDs for every TCP listener, from one netstat pass."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ports = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            local = parts[1]
            if ":" not in local:
                continue
            try:
                port = int(local.rsplit(":", 1)[1])
                pid = int(parts[4])
            except ValueError:
                continue
            ports.setdefault(port, set()).add(pid)
    return ports


def image_name(pid):
    """Image name for a PID via tasklist, or None if the process is gone."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[1].strip() == str(pid):
            return row[0]
    return None


def tail_lines(path, n, max_bytes=LOG_TAIL_BYTES):
    """Last n lines of a file, reading at most max_bytes from the end."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace").splitlines()[-n:]


class Hub:
    def __init__(self, port):
        self.port = port
        self.boot_time = time.time()
        self.lock = threading.Lock()
        self.start_requested = {}  # id -> epoch of last hub-initiated start
        self.up_since = {}         # id -> epoch first seen up by this hub
        self._registry_mtime = 0.0
        self.load_registry()
        for s in self.services:
            if s.get("self"):
                self.up_since[s["id"]] = self.boot_time

    def load_registry(self):
        """(Re)read services.json. Runs at boot and again whenever snapshot()
        sees the file's mtime change, so adding a service never needs a hub
        restart."""
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        for s in reg["services"]:
            if s.get("self"):
                s["port"] = self.port
                s["url"] = f"http://127.0.0.1:{self.port}/"
        with self.lock:
            self.registry = reg
            self.services = reg["services"]
            self.tools = reg.get("tools", [])
            self.by_id = {s["id"]: s for s in self.services}
            # Drop bookkeeping for services that left the registry.
            live = set(self.by_id)
            self.start_requested = {
                k: v for k, v in self.start_requested.items() if k in live}
            self.up_since = {
                k: v for k, v in self.up_since.items() if k in live}
        try:
            self._registry_mtime = REGISTRY_PATH.stat().st_mtime
        except OSError:
            self._registry_mtime = 0.0

    def maybe_reload_registry(self):
        try:
            mt = REGISTRY_PATH.stat().st_mtime
        except OSError:
            return
        if mt != self._registry_mtime:
            try:
                self.load_registry()
                print("services.json changed - registry reloaded")
            except (OSError, json.JSONDecodeError) as e:
                # Half-written file: keep the old registry, retry next poll.
                print(f"services.json reload failed ({e}); keeping old registry")

    def snapshot(self):
        self.maybe_reload_registry()
        portmap = listeners_by_port()
        now = time.time()
        rows = []
        for s in self.services:
            pids = sorted(portmap.get(s["port"], ()))
            listeners = [
                {"pid": pid, "image": image_name(pid) or "?"} for pid in pids
            ]
            up = port_open(s["port"])
            with self.lock:
                if up:
                    state = "up"
                    since = self.up_since.setdefault(s["id"], now)
                elif now - self.start_requested.get(s["id"], 0) < START_GRACE_SECONDS:
                    state = "starting"
                    since = None
                else:
                    state = "down"
                    self.up_since.pop(s["id"], None)
                    since = None
            rows.append({
                "id": s["id"],
                "name": s["name"],
                "url": s["url"],
                "port": s["port"],
                "description": s["description"],
                "managed": bool(s.get("start")),
                "is_self": bool(s.get("self")),
                "state": state,
                "listeners": listeners,
                "up_since": since,
            })
        return {"services": rows, "tools": self.tools, "now": now}

    def start(self, sid):
        s = self.by_id.get(sid)
        if not s or not s.get("start"):
            return 400, {"ok": False, "error": "not a managed service"}
        if port_open(s["port"]):
            with self.lock:
                self.up_since.setdefault(sid, time.time())
            return 200, {"ok": True, "note": "already running"}
        spec = s["start"]
        cmd = [sys.executable if a == "python" else a for a in spec["command"]]
        cwd = PROJECTS / spec["cwd"]
        if not cwd.is_dir():
            return 500, {"ok": False, "error": f"missing directory {cwd}"}
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"  # keep cp1252 consoles from killing children on accented output
        env.update(spec.get("env", {}))
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = NO_WINDOW
        logf = self._open_service_log(sid)
        try:
            subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            logf.close()
            return 500, {"ok": False, "error": str(exc)}
        logf.close()  # the child keeps its inherited handle
        with self.lock:
            self.start_requested[sid] = time.time()
        return 200, {"ok": True, "note": "start requested"}

    def stop(self, sid):
        s = self.by_id.get(sid)
        if not s:
            return 400, {"ok": False, "error": "unknown service"}
        if s.get("self"):
            return 400, {"ok": False, "error": "the hub will not stop itself"}
        pids = sorted(listeners_by_port().get(s["port"], ()))
        if not pids:
            with self.lock:
                self.up_since.pop(sid, None)
            return 200, {"ok": True, "note": "not running"}
        killed, refused = [], []
        for pid in pids:
            image = image_name(pid)
            if image is None:
                continue
            if image.lower() in KILLABLE_IMAGES:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=15, creationflags=NO_WINDOW,
                )
                killed.append({"pid": pid, "image": image})
            else:
                refused.append({"pid": pid, "image": image})
        deadline = time.time() + STOP_WAIT_SECONDS
        while time.time() < deadline and port_open(s["port"]):
            time.sleep(0.3)
        still_up = port_open(s["port"])
        with self.lock:
            if not still_up:
                self.up_since.pop(sid, None)
                self.start_requested.pop(sid, None)
        result = {"ok": not refused and not still_up,
                  "killed": killed, "still_up": still_up}
        if refused:
            result["refused"] = refused
            result["error"] = ("refused to kill non-project listener(s): "
                               + ", ".join(f"{r['image']} (pid {r['pid']})" for r in refused))
        return (200 if result["ok"] else 409), result

    def restart(self, sid):
        code, res = self.stop(sid)
        if code != 200 or not res.get("ok"):
            return code, res
        return self.start(sid)

    def start_all(self):
        results = {}
        for s in self.services:
            if s.get("self") or not s.get("start"):
                continue
            _, res = self.start(s["id"])
            results[s["id"]] = res
            time.sleep(0.3)
        return results

    def stop_all(self):
        results = {}
        for s in self.services:
            if s.get("self") or not s.get("start"):
                continue
            _, res = self.stop(s["id"])
            results[s["id"]] = res
        return results

    def _open_service_log(self, sid):
        """Append-mode handle for the service's captured console output, with
        a start banner and a size cap so a chatty service cannot grow it
        forever. The file lives in logs/<id>.log next to hub.py."""
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{sid}.log"
        try:
            if path.stat().st_size > LOG_ROTATE_BYTES:
                tail = path.read_bytes()[-LOG_ROTATE_BYTES // 4:]
                path.write_bytes(b"... hub truncated older output ...\n" + tail)
        except OSError:
            pass
        f = open(path, "ab")
        f.write(f"\n===== hub start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
        f.flush()
        return f

    def _service_log_path(self, s):
        """A service's own log, if services.json says where it lives:
        'log' is a fixed path relative to C:\\Projects, 'log_instance' is a
        KEY=value file whose LOG= line holds it (VocalCoach)."""
        if s.get("log"):
            return PROJECTS / s["log"]
        if s.get("log_instance"):
            try:
                for line in (PROJECTS / s["log_instance"]).read_text(
                        encoding="ascii", errors="replace").splitlines():
                    if line.startswith("LOG="):
                        return Path(line[4:].strip())
            except OSError:
                pass
        return None

    def log_tail(self, sid, lines=150):
        s = self.by_id.get(sid)
        if not s:
            return 404, {"ok": False, "error": f"unknown service {sid}"}
        try:
            lines = max(1, min(int(lines), LOG_TAIL_MAX))
        except (TypeError, ValueError):
            lines = 150
        sources = []
        own = self._service_log_path(s)
        if own and own.exists():
            sources.append({"label": "service log", "path": str(own),
                            "lines": tail_lines(own, lines)})
        hub_log = LOG_DIR / f"{sid}.log"
        if hub_log.exists():
            sources.append({"label": "console output", "path": str(hub_log),
                            "lines": tail_lines(hub_log, lines)})
        return 200, {"ok": True, "sources": sources}


class Handler(BaseHTTPRequestHandler):
    hub = None
    server_version = "project-hub"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # client went away mid-write; a closed tab aborts the poll

    def _html(self, path):
        body = path.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def do_GET(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if url.path == "/":
            self._html(INDEX_HTML)
        elif url.path == "/api/ping":
            self._json(200, {"app": "project-hub", "port": self.hub.port})
        elif url.path == "/api/status":
            self._json(200, self.hub.snapshot())
        elif len(parts) == 4 and parts[:2] == ["api", "services"] and parts[3] == "log":
            lines = parse_qs(url.query).get("lines", ["150"])[0]
            code, res = self.hub.log_tail(parts[2], lines)
            self._json(code, res)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("X-Hub-Request") != "1":
            self._json(403, {"ok": False, "error": "missing X-Hub-Request header"})
            return
        parts = self.path.strip("/").split("/")
        if parts == ["api", "start-all"]:
            self._json(200, {"ok": True, "results": self.hub.start_all()})
        elif parts == ["api", "shutdown"]:
            self._json(200, {"ok": True, "note": "hub shutting down"})
            threading.Timer(0.4, self.server.shutdown).start()
        elif parts == ["api", "stop-all"]:
            self._json(200, {"ok": True, "results": self.hub.stop_all()})
        elif len(parts) == 4 and parts[:2] == ["api", "services"] and parts[3] in ("start", "stop", "restart"):
            sid, action = parts[2], parts[3]
            if sid not in self.hub.by_id:
                self._json(404, {"ok": False, "error": f"unknown service {sid}"})
                return
            code, res = getattr(self.hub, action)(sid)
            self._json(code, res)
        else:
            self.send_error(404)


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that vanished mid-response (closed tab aborting the 2.5 s
        # poll) surfaces here as WinError 10053/10054 - noise, not a fault.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def already_running(port):
    """True if a hub is already answering on the port. A second hub must not
    bind: on Windows SO_REUSEADDR lets two listeners share one port."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/ping")
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status == 200 and b'"project-hub"' in data
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="project-hub service index")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--start-all", action="store_true",
                    help="start every managed service once the hub is up")
    args = ap.parse_args()

    if sys.stdout is None:
        # pythonw (start-hub-hidden.vbs) has no console - keep a log instead.
        LOG_DIR.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(
            LOG_DIR / "hub.log", "a", encoding="utf-8", buffering=1)

    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):
        print(f"project-hub is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    hub = Hub(args.port)
    Handler.hub = hub
    server = HubHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"project-hub: {url}")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if args.start_all:
        threading.Thread(target=hub.start_all, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
