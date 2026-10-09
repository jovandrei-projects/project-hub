"""project-hub tests: registry loading, log handling, storage report, the
kill-by-port lifecycle on a real spawned service, and a live-server pass.

The start/stop tests spawn a real `python -m http.server` on a test port and
let the hub kill it - the exact path the dashboard uses."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hub

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
slow = unittest.skipIf(os.environ.get('QUICK_TESTS'), 'skipped by --quick')
TEST_PORT = 18760
SVC_PORT = 18761


def fixture_registry(extra=None):
    services = [
        {"id": "hub", "name": "project-hub", "description": "self",
         "port": 8760, "url": "http://127.0.0.1:8760/", "self": True},
        {"id": "t-svc", "name": "test service", "description": "fixture",
         "port": SVC_PORT, "url": "http://127.0.0.1:%d/" % SVC_PORT,
         "start": {"command": ["python", "-m", "http.server", str(SVC_PORT),
                               "--bind", "127.0.0.1"],
                   "cwd": "project-hub"}},
    ]
    if extra:
        services += extra
    return {"services": services,
            "tools": [{"name": "a tool", "description": "d", "entry": "x"}]}


class hub_fixture:
    """A Hub over a temp services.json with LOG_DIR pointed at tmp."""

    def __init__(self, port=TEST_PORT, extra=None):
        self.tmp = tempfile.mkdtemp()
        reg = Path(self.tmp) / "services.json"
        reg.write_text(json.dumps(fixture_registry(extra)),
                       encoding="utf-8")
        logdir = Path(self.tmp) / "logs"
        self._reg = mock.patch.object(hub, "REGISTRY_PATH", reg)
        self._log = mock.patch.object(hub, "LOG_DIR", logdir)
        self._reg.start()
        self._log.start()
        self.hub = hub.Hub(port)

    def cleanup(self):
        self._reg.stop()
        self._log.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_port(port, want_open, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hub.port_open(port) == want_open:
            return True
        time.sleep(0.3)
    return False


class Helpers(unittest.TestCase):
    def test_port_open(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            self.assertTrue(hub.port_open(port))
        finally:
            sock.close()
        self.assertFalse(hub.port_open(port))

    def test_tail_lines(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / "x.log"
            p.write_bytes(b"".join(b"line%d\n" % i for i in range(50)))
            self.assertEqual(["line48", "line49"], hub.tail_lines(p, 2))
            # byte cap still returns whole last lines only
            capped = hub.tail_lines(p, 50, max_bytes=40)
            self.assertLessEqual(len("".join(capped)), 40)
            self.assertEqual([], hub.tail_lines(Path(tmp) / "gone", 5))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_listeners_by_port_finds_ours(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(SVC_PORT), "--bind",
             "127.0.0.1"], cwd=HERE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_port(SVC_PORT, True))
            pids = hub.listeners_by_port().get(SVC_PORT, set())
            self.assertIn(proc.pid, pids)
            self.assertIn("python", (hub.image_name(proc.pid) or "").lower())
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_already_running_only_for_hub(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(SVC_PORT), "--bind",
             "127.0.0.1"], cwd=HERE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_port(SVC_PORT, True))
            self.assertFalse(hub.already_running(SVC_PORT))
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class Registry(unittest.TestCase):
    def test_load_and_self_rewrite(self):
        f = hub_fixture(port=TEST_PORT)
        try:
            h = f.hub
            self.assertIn("t-svc", h.by_id)
            self.assertEqual(TEST_PORT, h.by_id["hub"]["port"])
            self.assertEqual("http://127.0.0.1:%d/" % TEST_PORT,
                             h.by_id["hub"]["url"])
            self.assertEqual(1, len(h.tools))
            # the self row counts as up since boot
            self.assertIsNotNone(h.up_since.get("hub"))
        finally:
            f.cleanup()

    def test_reload_on_mtime_change(self):
        f = hub_fixture()
        try:
            h = f.hub
            time.sleep(0.05)
            reg = dict(fixture_registry())
            reg["services"].append(
                {"id": "late", "name": "added later", "description": "x",
                 "port": 19999, "url": "http://127.0.0.1:19999/"})
            Path(f.tmp, "services.json").write_text(json.dumps(reg))
            os.utime(Path(f.tmp, "services.json"))
            h.maybe_reload_registry()
            self.assertIn("late", h.by_id)
        finally:
            f.cleanup()

    def test_broken_json_keeps_old_registry(self):
        f = hub_fixture()
        try:
            h = f.hub
            Path(f.tmp, "services.json").write_text("{ not json")
            h.maybe_reload_registry()
            self.assertIn("t-svc", h.by_id)   # previous registry survives
        finally:
            f.cleanup()


class Lifecycle(unittest.TestCase):
    @slow
    def test_start_stop_kill_by_port(self):
        f = hub_fixture()
        try:
            h = f.hub
            self.assertFalse(hub.port_open(SVC_PORT))
            code, res = h.start("t-svc")
            self.assertEqual(200, code, res)
            self.assertTrue(wait_port(SVC_PORT, True), "service never came up")
            # start is idempotent on a live port
            code, res = h.start("t-svc")
            self.assertEqual(200, code)
            self.assertEqual("already running", res["note"])
            snap = h.snapshot()
            row = {s["id"]: s for s in snap["services"]}["t-svc"]
            self.assertEqual("up", row["state"])
            self.assertTrue(row["listeners"])
            code, res = h.stop("t-svc")
            self.assertEqual(200, code, res)
            self.assertTrue(res["killed"])
            self.assertFalse(hub.port_open(SVC_PORT))
        finally:
            f.cleanup()

    @slow
    def test_stop_refuses_foreign_image(self):
        f = hub_fixture()
        try:
            h = f.hub
            code, res = h.start("t-svc")
            self.assertEqual(200, code, res)
            self.assertTrue(wait_port(SVC_PORT, True))
            pid = sorted(hub.listeners_by_port().get(SVC_PORT))[0]
            # pretend the listener is a browser - must NOT be killed
            with mock.patch.object(hub, "image_name",
                                   return_value="chrome.exe"):
                code, res = h.stop("t-svc")
            self.assertEqual(409, code)
            self.assertIn("refused", res)
            self.assertTrue(hub.port_open(SVC_PORT),
                            "foreign listener must survive stop()")
        finally:
            # cleanup: actually stop it
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
            f.cleanup()

    def test_unmanaged_and_self_guards(self):
        f = hub_fixture()
        try:
            h = f.hub
            code, _ = h.start("hub")
            self.assertEqual(400, code)              # self has no start spec
            code, res = h.stop("hub")
            self.assertEqual(400, code)
            self.assertIn("itself", res["error"])
            code, _ = h.stop("nosuch")
            self.assertEqual(400, code)
            code, _ = h.start("nosuch")
            self.assertEqual(400, code)
        finally:
            f.cleanup()


class LogsAndStorage(unittest.TestCase):
    def test_service_log_rotation_and_tail(self):
        f = hub_fixture()
        try:
            h = f.hub
            logdir = Path(f.tmp) / "logs"
            with mock.patch.object(hub, "LOG_ROTATE_BYTES", 2048):
                big = logdir / "t-svc.log"
                logdir.mkdir(exist_ok=True)
                big.write_bytes(b"x" * 5000)
                fh = h._open_service_log("t-svc")
                fh.close()
                data = big.read_bytes()
                self.assertLess(len(data), 2048 + 200)
                self.assertIn(b"hub truncated", data)
                self.assertIn(b"hub start", data)
            code, res = h.log_tail("t-svc")
            self.assertEqual(200, code)
            self.assertTrue(res["sources"])
            code, _ = h.log_tail("nosuch")
            self.assertEqual(404, code)
        finally:
            f.cleanup()

    def test_storage_report(self):
        tmp = tempfile.mkdtemp()
        try:
            proj = Path(tmp) / "fake-proj"
            (proj / "logs").mkdir(parents=True)
            (proj / "data").mkdir()
            (proj / "logs" / "a.log").write_bytes(b"x" * 100)
            (proj / "data" / "b.bin").write_bytes(b"y" * 200)
            with mock.patch.object(hub, "PROJECTS", Path(tmp)):
                rep = hub.storage_report()
            row = {p["project"]: p for p in rep["projects"]}["fake-proj"]
            self.assertEqual(100, row["logs"]["bytes"])
            self.assertEqual(200, row["data"]["bytes"])
            self.assertGreater(rep["disk"]["free"], 0)
            self.assertIn("warn_logs_bytes", rep)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LiveSmoke(unittest.TestCase):
    @slow
    def test_hub_serves(self):
        proc = subprocess.Popen(
            [sys.executable, "hub.py", "--port", str(TEST_PORT),
             "--no-browser"], cwd=HERE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = "http://127.0.0.1:%d" % TEST_PORT
            ping = None
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(base + "/api/ping",
                                                timeout=3) as r:
                        ping = json.loads(r.read())
                    break
                except Exception:
                    if proc.poll() is not None:
                        self.fail("hub exited: %s" % proc.returncode)
                    time.sleep(0.5)
            self.assertEqual("project-hub", ping["app"])

            with urllib.request.urlopen(base + "/api/status",
                                        timeout=15) as r:
                status = json.loads(r.read())
            self.assertIn("services", status)
            self.assertIn("money-plan",
                          {s["id"] for s in status["services"]})

            with urllib.request.urlopen(base + "/api/storage",
                                        timeout=60) as r:
                storage = json.loads(r.read())
            self.assertIn("disk", storage)
            self.assertIn("projects", storage)

            # POST guard
            req = urllib.request.Request(base + "/api/stop-all",
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(403, ctx.exception.code)

            # graceful shutdown via the API (the real Stop hub button path)
            req = urllib.request.Request(
                base + "/api/shutdown", method="POST",
                headers={"X-Hub-Request": "1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(200, r.status)
            proc.wait(timeout=15)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
