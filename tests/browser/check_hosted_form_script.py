"""A REAL-BROWSER check of the hosted form's script (``hosted/public/static/app.js``), on
INVENTED data. Not a pytest test (the test client runs no script); a development tool, outside
``hosted/`` (the folder that is deployed).

    PYTHONPATH=src .venv/bin/python tests/browser/check_hosted_form_script.py [--port 8621]
        [--keep-shots DIR]

It starts the development server on the demo (``scripts/hosted_dev_server.py``, signed in as the
invented reviewer REMOTE-2; the server opens its invented reviewers for saving itself) and
headless Google Chrome, and answers the demo's form i07 as a
person does: real mouse clicks on radio buttons and check boxes, words and prices typed key by key
into every one-line box, Tab and Enter out of them, a pause inside a box long enough for the
round trip to come back while the box still has the focus (finding A of the acceptance), a list
box, the notes, a mark "For later". After every step it checks that every answer given so far is
still on the page, and after every Tab that the focus went on to a control (finding B). At the
end it reloads the page: the form comes back from the kept draft and must equal what was
entered. Prints PASS or FAIL with what differs; exit code 0 or 1. Everything it starts is
stopped and deleted when it ends.

It FAILS on build 72509cd (the typed stop wipes every other answer and the wiped state is kept as
the draft) and PASSES once the script never reads or replaces the form at the wrong moment.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# The development server (it opens the demo's reviewers for saving itself).
BOOT = """
import importlib.util, sys
from pathlib import Path
repo = Path.cwd()
sys.path[:0] = [str(repo / "src"), str(repo / "hosted")]
# The preview's network: every round trip takes 0.4 s, as across the internet (on this machine it
# takes a few milliseconds, which hides the races a reviewer meets).
import time
from reviewer_web import app as web
create = web.create_app
def create_app(*args, **kw):
    made = create(*args, **kw)
    inner = made.wsgi_app
    def slow(environ, start_response):
        if environ.get("REQUEST_METHOD") == "POST" and environ.get("PATH_INFO", "").endswith("/state"):
            time.sleep(0.4)
        return inner(environ, start_response)
    made.wsgi_app = slow
    return made
web.create_app = create_app
spec = importlib.util.spec_from_file_location("dev", repo / "scripts" / "hosted_dev_server.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)
sys.argv = ["dev", "--port", sys.argv[1], "--dev-auto-sign-in", "REMOTE-2"]
dev.main()
"""

# (what, control name, value): the order a reviewer answers i07 (each answer makes the next
# question appear). "type" ends with how the box is left: "tab", "enter" or "wait" (the focus
# stays in the box while the round trip runs).
STEPS = [
    ("click", "timing", "CURRENT"), ("click", "action", "OPEN"), ("click", "explicit", "YES"),
    ("choose", "family", "OTHER"),
    ("click", "s.instrument_as_read", "VALUE"), ("type", "instrument_as_read", "Zed Metals", "tab"),
    ("click", "quoted_series", "SPOT_OR_INDEX"), ("click", "traded_instrument", "NOT_STATED"),
    ("click", "side", "SHORT"), ("click", "position_state", "FLAT"), ("click", "effect", "OPENS"),
    ("click", "s.entry_price", "VALUE"), ("type", "entry_price", "412", "enter"),
    ("click", "entry_role", "CURRENT_MARKET"), ("click", "entry_exactness", "APPROXIMATE"),
    ("click", "s.stop_price", "VALUE"), ("type", "stop_price", "420", "wait"),
    ("click", "s.targets", "VALUE"), ("type", "targets", "400, 390", "tab"),
    ("choose", "idea", "NEW_IDEA"), ("tick", "ambiguities", "PRICE_ROLE_UNCLEAR"),
    ("choose", "lv.n:0.role", "CURRENT_MARKET"), ("choose", "lv.n:0.exactness", "APPROXIMATE"),
    ("choose", "lv.n:1.role", "STOP"), ("choose", "lv.n:1.exactness", "EXACT"),
    ("tick", "ui.later", "field-timing"), ("click", "confidence", "MEDIUM"),
    ("type", "notes", "Checked the stop twice", "wait"),
]

READ = """(function(){const out={};
 for (const el of document.querySelectorAll('#form-body input, #form-body select, #form-body textarea')) {
   if (!el.name || el.type==='hidden' || el.name==='flags') continue;
   if (el.type==='radio') { if (el.checked) out[el.name]=el.value; else if (!(el.name in out)) out[el.name]=''; }
   else if (el.type==='checkbox') { out[el.name]=out[el.name]||[]; if (el.checked) out[el.name].push(el.value); }
   else out[el.name]=el.value;
 } return JSON.stringify(out);})()"""
FOCUS = """(function(){const a=document.activeElement; const body=document.getElementById('form-body');
 return JSON.stringify({inForm: !!(body && body.contains(a)), tag: a.tagName, name: a.name||'', id: a.id||''});})()"""


class Tab:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, **params):
        self.n += 1
        mine = self.n
        await self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mine:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True,
                            returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")

    async def quiet(self, at_least=0.9):
        """Wait until no round trip is running (the page's "Updating the form…" is empty)."""
        await asyncio.sleep(at_least)
        for _ in range(100):
            if not await self.js("(document.getElementById('busy')||{}).textContent"):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)

    async def click_at(self, selector, hold=0.0, on_words=False):
        """A real mouse click on the element (scrolled into view first): on its box, or with
        ``on_words`` on the words of its label, where a person usually clicks."""
        box = await self.js(f"""(function(){{const el=document.querySelector({json.dumps(selector)});
            if(!el) return null; el.scrollIntoView({{block:'center'}});
            const lab = el.closest('label') || (el.id && document.querySelector('label[for="'+el.id+'"]')) || el;
            const words = {json.dumps(on_words)} && lab.querySelector('span');
            const r=(words || lab).getBoundingClientRect();
            return [words ? r.left + r.width/2 : r.left+Math.min(12, r.width/2), r.top+r.height/2];}})()""")
        if box is None:
            return False
        await asyncio.sleep(0.15)
        for kind in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", type=kind, x=box[0], y=box[1],
                            button="left", clickCount=1)
            if kind == "mousePressed" and hold:
                await asyncio.sleep(hold)  # a finger rests on the button as a person's does
        return True

    async def key(self, key, code, vk):
        await self.call("Input.dispatchKeyEvent", type="rawKeyDown", key=key, code=code,
                        windowsVirtualKeyCode=vk)
        if key == "Enter":
            await self.call("Input.dispatchKeyEvent", type="char", key=key, text="\r")
        await self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code,
                        windowsVirtualKeyCode=vk)

    async def type_text(self, text):
        for ch in text:
            await self.call("Input.dispatchKeyEvent", type="keyDown", key=ch, text=ch)
            await self.call("Input.dispatchKeyEvent", type="keyUp", key=ch)
            await asyncio.sleep(0.06)


async def answer(tab: Tab, base: str, shots: Path | None) -> list[str]:
    problems: list[str] = []
    await tab.call("Page.navigate", url=base + "/form/i07")
    await tab.quiet(1.5)
    want: dict = {}
    for step in STEPS:
        what, name, value = step[:3]
        sel = f'#form-body [name="{name}"][value="{value}"]' if what in ("click", "tick") \
            else f'#form-body [name="{name}"]'
        if what in ("click", "tick"):
            if not await tab.click_at(sel):
                problems.append(f"no control {name}={value} (the form lost an answer before it)")
                break
            if what == "tick":
                want.setdefault(name, []).append(value)
            else:
                want[name] = value
            await tab.quiet()
        elif what == "choose":
            if not await tab.click_at(sel):
                problems.append(f"no list box {name}")
                break
            await tab.key("Escape", "Escape", 27)
            await tab.js(f"""(function(){{const el=document.querySelector({json.dumps(sel)});
                el.value={json.dumps(value)}; el.dispatchEvent(new Event('change',{{bubbles:true}}));}})()""")
            want[name] = value
            await tab.quiet()
        else:
            leave = step[3]
            if not await tab.click_at(sel):
                problems.append(f"no box {name}")
                break
            await tab.type_text(value)
            want[name] = value
            if leave == "tab":
                await tab.key("Tab", "Tab", 9)
                await tab.quiet()
                focus = json.loads(await tab.js(FOCUS))
                if not focus["inForm"]:
                    problems.append(f"after Tab out of {name} the focus left the form ({focus})")
            elif leave == "enter":
                await tab.key("Enter", "Enter", 13)
                await tab.quiet()
                focus = json.loads(await tab.js(FOCUS))
                if focus["name"] != name:
                    problems.append(f"after Enter in {name} the focus is not in it ({focus})")
            else:
                await tab.quiet(2.0)  # the round trip comes back with the focus in the box
                focus = json.loads(await tab.js(FOCUS))
                if focus["name"] != name:
                    problems.append(f"after a pause in {name} the focus moved ({focus})")
        got = json.loads(await tab.js(READ))
        lost = {k: (v, got.get(k)) for k, v in want.items() if got.get(k) != v}
        if lost:
            problems.append(f"after {what} {name}: lost {lost}")
            break
    # Finding 3 of the second run: the first click right after typing must not be lost. Type in
    # a one-line box (no Tab, no Enter), then click another answer after 0.6 to 1.6 seconds,
    # when the reply to the typing comes back.
    if not problems:
        for n, delay in enumerate((0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4, 1.6)):
            words = f"Zed Metals {n}"
            choice = "NO" if n % 2 == 0 else "YES"
            box = '#form-body [name="instrument_as_read"]'
            await tab.js(f"""(function(){{const el=document.querySelector({json.dumps(box)});
                el.scrollIntoView({{block:'center'}}); el.focus(); el.select();}})()""")
            await tab.type_text(words)
            await asyncio.sleep(delay)
            if not await tab.click_at(f'#form-body [name="explicit"][value="{choice}"]',
                                      hold=0.15, on_words=True):
                problems.append(f"after typing, at {delay} s: no answer to click")
                break
            want["instrument_as_read"], want["explicit"] = words, choice
            await tab.quiet(2.0)
            got = json.loads(await tab.js(READ))
            if got.get("explicit") != choice:
                problems.append(f"a click {delay} s after typing was lost (explicit is "
                                f"{got.get('explicit')!r}, not {choice!r})")
            if got.get("instrument_as_read") != words:
                problems.append(f"typed words lost around a click at {delay} s: "
                                f"{got.get('instrument_as_read')!r}")
    if shots:
        await shot(tab, shots / "check-before-reload.png")
    # The kept draft: a reload must bring back exactly what was entered.
    await tab.call("Page.reload")
    await tab.quiet(2.0)
    got = json.loads(await tab.js(READ))
    lost = {k: (v, got.get(k)) for k, v in want.items() if got.get(k) != v}
    if lost:
        problems.append(f"after a reload (the kept draft): lost {lost}")
    if "Draft kept at" not in (await tab.js("document.body.textContent") or ""):
        problems.append("after a reload the page does not say that a draft is kept")
    if shots:
        await shot(tab, shots / "check-after-reload.png")
    return problems


async def shot(tab: Tab, path: Path) -> None:
    r = await tab.call("Page.captureScreenshot", format="png")
    path.write_bytes(base64.b64decode(r["data"]))


def page_socket(debug: int) -> str:
    """The address of Chrome's first page, once Chrome answers."""
    for _ in range(100):
        try:
            pages = [t for t in json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{debug}/json")) if t["type"] == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit("FAIL: Chrome did not start")


async def drive(port: int, socket: str, shots: Path | None) -> list[str]:
    import websockets

    async with websockets.connect(socket, max_size=2**26) as ws:
        tab = Tab(ws)
        await tab.call("Page.enable")
        await tab.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                       deviceScaleFactor=1, mobile=False)
        return await answer(tab, f"http://127.0.0.1:{port}", shots)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8621)
    parser.add_argument("--keep-shots", type=Path)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="hosted-browser-check-"))
    server = subprocess.Popen(
        [sys.executable, "-c", BOOT, str(args.port)], cwd=REPO, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env={"PYTHONPATH": str(REPO / "src"),
                                                  "PATH": "/usr/bin:/bin"})
    chrome = None
    try:
        for line in server.stdout:
            if "Running on" in line:
                break
        else:
            print("FAIL: the development server did not start")
            return 1
        debug = args.port + 1000
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={debug}",
             f"--user-data-dir={work / 'chrome'}", "--no-first-run", "--hide-scrollbars",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if args.keep_shots:
            args.keep_shots.mkdir(parents=True, exist_ok=True)
        problems = asyncio.run(drive(args.port, page_socket(debug), args.keep_shots))
    finally:
        if chrome is not None:
            chrome.terminate()
        # An interrupt, as Ctrl-C: the server's own clean-up stops and deletes its database
        # (a plain terminate would leave the embedded Postgres running).
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
    print(f"PASS: {len(STEPS)} steps (every kind of answer, Tab, Enter and a pause in a box), "
          "the focus kept after every Tab, eight clicks 0.6 to 1.6 s after typing, and the "
          "reload equal to what was entered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
