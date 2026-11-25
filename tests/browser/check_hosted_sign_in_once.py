"""A REAL-BROWSER check that the code form of the sign-in is sent ONCE (the production incident
of 2026-11-26: "Sign in" pressed twice during the two-second wait; the first answer, with the
session, was dropped; the second met a used code). INVENTED data; a development tool, outside
``hosted/`` (the folder that is deployed).

    PYTHONPATH=src .venv/bin/python tests/browser/check_hosted_sign_in_once.py [--port 8631]

It starts the development server on the demo with the site's own two-second floor on every
sign-in step (the development server turns it off; this check turns it back on), and headless
Chrome at phone size. Twice (presses 0.7 s apart, then 1.9 s apart): enter the address, read the
code the server prints, type it, press "Sign in" twice (real pointer presses). Right after the
first press the button must be closed and say "Checking the code…" (aria-busy); the server's log
must show ONE request to ``/sign-in/verify``; the browser must end on the dashboard. (On this
machine headless Chrome against the local server itself sent only one request even on build
cdd34b0, unlike the phone of the incident, so the closed, busy button is what fails there.)
Between the two rounds it signs out. Prints PASS or FAIL; exit code
0 or 1. Everything it starts is stopped and deleted at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_hosted_form_script import CHROME, REPO, Tab, page_socket

DEBUG = "--debug" in sys.argv
ADDRESS = "reviewer@example.invalid"  # the demo's invented reviewer REMOTE-2
BOOT = """
import importlib.util, sys
from pathlib import Path
repo = Path.cwd()
sys.path[:0] = [str(repo / "src"), str(repo / "hosted")]
from reviewer_web.store import AdminStore
from reviewer_web import app as web
anchor = AdminStore.set_anchor
def set_anchor(self, *args, **kw):
    anchor(self, *args, **kw)
    with self.connection() as conn:
        conn.execute("UPDATE reviewer SET imported_at = now() WHERE imported_at IS NULL")
AdminStore.set_anchor = set_anchor
create = web.create_app
def create_app(store, mailer, config):
    return create(store, mailer, {**config, "SIGN_IN_FLOOR_SECONDS": 2.0})
web.create_app = create_app
spec = importlib.util.spec_from_file_location("dev", repo / "scripts" / "hosted_dev_server.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)
sys.argv = ["dev", "--port", sys.argv[1]]
dev.main()
"""


class Log:
    """Everything the server prints, read as it comes (a pipe left unread would stall it)."""

    def __init__(self, stream):
        self.lines: list[str] = []
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream):
        for line in stream:
            self.lines.append(line)

    def wait_for(self, pattern: str, after: int = 0, seconds: float = 60.0) -> re.Match | None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for line in self.lines[after:]:
                found = re.search(pattern, line)
                if found:
                    return found
            time.sleep(0.1)
        return None


async def press(tab: Tab, selector: str) -> bool:
    box = await tab.js(f"""(function(){{const el=document.querySelector({json.dumps(selector)});
        if(!el) return null; const r=el.getBoundingClientRect();
        return [r.left+r.width/2, r.top+r.height/2];}})()""")
    if box is None:
        return False
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await tab.call("Input.dispatchMouseEvent", type=kind, x=box[0], y=box[1], button="left",
                       buttons=1 if kind == "mousePressed" else 0,
                       clickCount=0 if kind == "mouseMoved" else 1)
    return True


async def round_trip(tab: Tab, base: str, log: Log, gap: float) -> list[str]:
    problems: list[str] = []
    await tab.call("Page.navigate", url=base + "/sign-in")
    await asyncio.sleep(1.0)
    await tab.js(f"document.getElementById('address').value={json.dumps(ADDRESS)}")
    mark = len(log.lines)
    await press(tab, "form[action='/sign-in'] button")
    code = log.wait_for(r"\b(\d{6})\b", mark)
    if code is None:
        return ["no code was printed by the server"]
    await asyncio.sleep(2.5)
    await tab.js(f"document.getElementById('code').value={json.dumps(code.group(1))}")
    mark = len(log.lines)
    # What the page shows just after the first submit, kept for the next page to read.
    await tab.js("""sessionStorage.removeItem('busy'); document.addEventListener('submit', e => {
        setTimeout(() => { const b = e.target.querySelector('button');
          sessionStorage.setItem('busy', JSON.stringify([b.disabled, b.getAttribute('aria-busy'),
            b.textContent.trim()])); }, 50); });""")
    await press(tab, "form[action='/sign-in/verify'] button")
    await asyncio.sleep(0.3)
    await asyncio.sleep(max(0.0, gap - 0.3))
    await press(tab, "form[action='/sign-in/verify'] button")
    await asyncio.sleep(5.0)
    sent = sum("POST /sign-in/verify" in line for line in log.lines[mark:])
    busy = await tab.js("sessionStorage.getItem('busy')")
    state = json.loads(busy) if busy else None
    if state is None or not state[0] or state[1] != "true" or "Checking" not in state[2]:
        problems.append(f"right after the first press the button was not closed and busy "
                        f"(disabled, aria-busy, words): {state}")
    if DEBUG:
        print("".join(line for line in log.lines[mark:] if "sign-in" in line or "GET /" in line))
    where = await tab.js("location.pathname + ' | ' + document.title")
    if sent != 1:
        problems.append(f"presses {gap} s apart sent {sent} requests to /sign-in/verify")
    if not where.startswith("/ |"):
        problems.append(f"presses {gap} s apart ended on {where!r}, not the dashboard")
    return problems


async def drive(port: int, socket: str, log: Log) -> list[str]:
    import websockets

    async with websockets.connect(socket, max_size=2**26) as ws:
        tab = Tab(ws)
        await tab.call("Page.enable")
        await tab.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                       deviceScaleFactor=2, mobile=True)
        base = f"http://127.0.0.1:{port}"
        problems: list[str] = []
        for gap in (0.7, 1.9):
            problems += await round_trip(tab, base, log, gap)
            await tab.call("Network.enable")
            await tab.call("Network.clearBrowserCookies")  # signed out: the next round anew
        return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8631)
    parser.add_argument("--debug", action="store_true", help="print the sign-in lines of the log")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="hosted-sign-in-check-"))
    server = subprocess.Popen([sys.executable, "-c", BOOT, str(args.port)], cwd=REPO,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"})
    log = Log(server.stdout)
    chrome = None
    try:
        if log.wait_for("Running on") is None:
            print("FAIL: the development server did not start")
            return 1
        debug = args.port + 1000
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={debug}",
             f"--user-data-dir={work / 'chrome'}", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        problems = asyncio.run(drive(args.port, page_socket(debug), log))
    finally:
        if chrome is not None:
            chrome.terminate()
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(work, ignore_errors=True)
    if problems:
        print("FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print("PASS: two presses on \"Sign in\", 0.7 s and 1.9 s apart: the button closed and busy "
          "after the first, ONE request each, and the dashboard opened.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
