"""Browser smoke: the hub page in real headless Chromium via Playwright.

Spawns hub.py on a test port, loads the page, waits for the service table
to populate, opens one service's Log panel (read-only), and fails on any
uncaught JS error. Skipped under --quick and when playwright or its
browser binaries are absent (python -m playwright install chromium).
"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
slow = unittest.skipIf(os.environ.get('QUICK_TESTS'), 'skipped by --quick')
TEST_PORT = 18860


def wait_http(url, proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                r.read()
            return
        except Exception:
            if proc.poll() is not None:
                raise AssertionError('hub exited during startup: %s'
                                     % proc.returncode)
            time.sleep(0.5)
    raise AssertionError('hub never answered %s' % url)


class Browser(object):
    """Headless chromium page that records uncaught JS errors."""

    def __init__(self):
        self.errors = []
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch(headless=True)
        except Exception as exc:
            self._pw.stop()
            raise unittest.SkipTest(
                'chromium not launchable (python -m playwright install '
                'chromium): %s' % exc)
        self.page = self.browser.new_page()
        self.page.on('pageerror',
                     lambda e: self.errors.append('pageerror: %s' % e))
        self.page.on('console', self._console)

    def _console(self, m):
        # 'Failed to load resource' is network noise (404/503 fetch), not a
        # JS defect - real console.error calls still land here.
        if m.type == 'error' and 'Failed to load resource' not in m.text:
            self.errors.append('console.error: %s' % m.text)

    def close(self):
        self.browser.close()
        self._pw.stop()


@unittest.skipUnless(sync_playwright, 'playwright not installed')
class HubPage(unittest.TestCase):
    @slow
    def test_page_boots_and_log_panel_opens(self):
        proc = subprocess.Popen(
            [sys.executable, 'hub.py', '--port', str(TEST_PORT),
             '--no-browser'], cwd=HERE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = 'http://127.0.0.1:%d' % TEST_PORT
            wait_http(base + '/api/ping', proc)
            b = Browser()
            try:
                b.page.goto(base + '/', timeout=30000)
                # service rows render from /api/status polling; a Log
                # button only exists once a real row has rendered
                b.page.wait_for_selector('#rows button:has-text("Log")',
                                         timeout=20000)
                self.assertIn('Projects', b.page.title())
                names = b.page.locator('#rows').inner_text()
                self.assertIn('money-plan', names)

                # read-only click: first service's Log tail panel
                b.page.locator('#rows button:has-text("Log")').first.click()
                b.page.wait_for_function(
                    "document.getElementById('logbody')"
                    " && document.getElementById('logbody')"
                    "   .textContent.length > 0",
                    timeout=10000)
            finally:
                errs = b.errors
                b.close()
            self.assertEqual([], errs)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == '__main__':
    unittest.main()
