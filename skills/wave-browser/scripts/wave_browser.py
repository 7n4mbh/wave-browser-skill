#!/usr/bin/env python3
"""wave-browser: a Playwright-driven browser session for Wave Terminal.

Architecture
------------
* `start`  spawns a detached daemon process that:
    - launches headless Chromium via Playwright
    - opens a CDP session and calls `Page.startScreencast` (JPEG frames)
    - runs a tiny aiohttp server that
        - serves a viewer HTML page on `/`
        - pushes screencast frames over `/ws`
        - accepts JSON action commands on `/action`
    - registers the local viewer URL in Wave Terminal via `wsh view`
* The CLI's other subcommands talk to the daemon over HTTP, so the agent can
  drive the browser through stateless `wave-browser <verb> ...` calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

try:
    from aiohttp import web
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        async_playwright,
    )
except ImportError:
    if len(sys.argv) >= 2 and sys.argv[1] in ("__serve__", "__daemon__"):
        sys.stderr.write(
            "[wave-browser] missing python deps. Run scripts/bootstrap.sh first.\n"
        )
        sys.exit(2)
    web = None  # type: ignore
    async_playwright = None  # type: ignore


SESSION_DIR = Path(os.environ.get("WAVE_BROWSER_HOME", str(Path.home() / ".wave-browser")))
SESSION_FILE = SESSION_DIR / "session.json"
LOG_FILE = SESSION_DIR / "daemon.log"
DEFAULT_VIEWPORT = (1280, 800)

# Lifecycle. The serve process is normally launched *inside a Wave terminal
# block* via `wsh run`, so closing that block sends SIGTERM/SIGHUP and the
# process dies immediately -- that is the primary mechanism. The values below
# are a secondary safety net for two cases:
#  (a) the viewer preview block never connects (e.g. `wsh view` failed)
#  (b) the user closed the *viewer* block but kept the *terminal* block
#      around -- we don't want a headless Chromium running invisibly.
# Set IDLE_TIMEOUT=0 to disable the safety net entirely.
STARTUP_GRACE = float(os.environ.get("WAVE_BROWSER_STARTUP_GRACE", "60"))
IDLE_TIMEOUT = float(os.environ.get("WAVE_BROWSER_IDLE_TIMEOUT", "10"))


VIEWER_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8"/>
<title>wave-browser</title>
<style>
  html,body{margin:0;padding:0;background:#0b0b0d;height:100%;overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e6e6e6}
  .bar{position:fixed;top:0;left:0;right:0;height:32px;display:flex;align-items:center;
    gap:10px;padding:0 12px;background:rgba(18,18,22,.92);
    border-bottom:1px solid #222;font-size:12px;z-index:10}
  .dot{width:8px;height:8px;border-radius:50%;background:#3aa55a;transition:background .2s}
  .dot.off{background:#a33}
  .url{flex:1;color:#7fb6ff;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
  .status{color:#888}
  .stage{position:absolute;top:32px;left:0;right:0;bottom:0;
    display:flex;align-items:center;justify-content:center;padding:8px}
  img{max-width:100%;max-height:100%;object-fit:contain;background:#111;
    box-shadow:0 6px 30px rgba(0,0,0,.55);border-radius:4px}
  .idle{color:#666;font-size:13px}
</style>
</head><body>
<div class="bar">
  <div class="dot" id="dot"></div>
  <div><strong>wave-browser</strong></div>
  <div class="url" id="url">--</div>
  <div class="status" id="status">connecting…</div>
</div>
<div class="stage"><img id="screen" alt=""/><div class="idle" id="idle" style="display:none">waiting for first frame…</div></div>
<script>
  const img=document.getElementById('screen');
  const dot=document.getElementById('dot');
  const urlEl=document.getElementById('url');
  const statusEl=document.getElementById('status');
  const idle=document.getElementById('idle');
  let gotFrame=false;
  function connect(){
    const ws=new WebSocket('ws://'+location.host+'/ws');
    ws.onopen=()=>{dot.classList.remove('off');statusEl.textContent='live';if(!gotFrame)idle.style.display='block';};
    ws.onclose=()=>{dot.classList.add('off');statusEl.textContent='reconnecting…';setTimeout(connect,800);};
    ws.onerror=()=>{};
    ws.onmessage=(ev)=>{
      try{
        const msg=JSON.parse(ev.data);
        if(msg.frame){img.src='data:image/jpeg;base64,'+msg.frame;gotFrame=true;idle.style.display='none';}
        if(typeof msg.url==='string'){urlEl.textContent=msg.url||'about:blank';}
        if(typeof msg.title==='string'&&msg.title){document.title='wave-browser -- '+msg.title;}
      }catch(e){}
    };
  }
  connect();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------

class Session:
    def __init__(self) -> None:
        self.pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.cdp = None
        self.clients: set[Any] = set()
        self.latest_frame: Optional[str] = None
        self.latest_url: str = "about:blank"
        self.latest_title: str = ""
        self.action_lock = asyncio.Lock()
        self.first_client_seen: bool = False
        self.last_activity: float = time.time()

    async def start(self, start_url: str, viewport: tuple[int, int] = DEFAULT_VIEWPORT) -> None:
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()

        def _on_nav(frame):
            if self.page and frame == self.page.main_frame:
                self.latest_url = frame.url
                asyncio.create_task(self._broadcast({"url": frame.url}))

        self.page.on("framenavigated", _on_nav)

        self.cdp = await self.context.new_cdp_session(self.page)

        async def on_frame(params):
            self.latest_frame = params["data"]
            await self._broadcast({"frame": params["data"]})
            try:
                await self.cdp.send(
                    "Page.screencastFrameAck",
                    {"sessionId": params["sessionId"]},
                )
            except Exception:
                pass

        self.cdp.on("Page.screencastFrame", on_frame)
        await self.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 70, "everyNthFrame": 1},
        )

        if start_url and start_url != "about:blank":
            try:
                await self.page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                sys.stderr.write(f"[wave-browser] initial goto failed: {e}\n")

    async def _broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def stop(self) -> None:
        try:
            if self.cdp:
                await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass

    async def refresh_title(self) -> None:
        if self.page:
            try:
                self.latest_title = await self.page.title()
            except Exception:
                pass

    async def do(self, cmd: str, args: dict) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "error": "browser not initialized"}
        try:
            if cmd == "goto":
                await page.goto(
                    args["url"],
                    wait_until=args.get("wait_until", "domcontentloaded"),
                    timeout=args.get("timeout", 30000),
                )
                await self.refresh_title()
                return {"ok": True, "url": page.url, "title": self.latest_title}

            if cmd == "click":
                loc = page.locator(args["selector"]).first
                await loc.click(timeout=args.get("timeout", 10000))
                return {"ok": True}

            if cmd == "fill":
                loc = page.locator(args["selector"]).first
                await loc.fill(args["value"], timeout=args.get("timeout", 10000))
                return {"ok": True}

            if cmd == "type":
                await page.keyboard.type(args["text"], delay=args.get("delay", 30))
                return {"ok": True}

            if cmd == "press":
                await page.keyboard.press(args["key"])
                return {"ok": True}

            if cmd == "wait":
                loc = page.locator(args["selector"]).first
                await loc.wait_for(
                    state=args.get("state", "visible"),
                    timeout=args.get("timeout", 30000),
                )
                return {"ok": True}

            if cmd == "sleep":
                await asyncio.sleep(float(args.get("seconds", 1)))
                return {"ok": True}

            if cmd == "scroll":
                direction = args.get("direction", "down")
                amount = int(args.get("amount", 600))
                if direction == "down":
                    await page.mouse.wheel(0, amount)
                elif direction == "up":
                    await page.mouse.wheel(0, -amount)
                elif direction == "to" and args.get("selector"):
                    await page.locator(args["selector"]).first.scroll_into_view_if_needed()
                return {"ok": True}

            if cmd == "text":
                txt = await page.locator(args["selector"]).first.text_content(
                    timeout=args.get("timeout", 10000)
                )
                return {"ok": True, "text": txt}

            if cmd == "html":
                content = await page.content()
                limit = int(args.get("limit", 200000))
                return {"ok": True, "html": content[:limit], "truncated": len(content) > limit}

            if cmd == "title":
                return {"ok": True, "title": await page.title()}

            if cmd == "url":
                return {"ok": True, "url": page.url}

            if cmd == "eval":
                result = await page.evaluate(args["expression"])
                return {"ok": True, "result": result}

            if cmd == "screenshot":
                path = args.get("path") or str(SESSION_DIR / f"shot-{int(time.time())}.png")
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=path, full_page=bool(args.get("full_page", False)))
                return {"ok": True, "path": path}

            if cmd == "back":
                await page.go_back()
                return {"ok": True, "url": page.url}

            if cmd == "forward":
                await page.go_forward()
                return {"ok": True, "url": page.url}

            if cmd == "reload":
                await page.reload()
                return {"ok": True, "url": page.url}

            if cmd == "links":
                links = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                        .slice(0, 300)
                        .map(a => ({text: (a.innerText||'').trim().slice(0,120), href: a.href}))"""
                )
                return {"ok": True, "links": links}

            if cmd == "summary":
                data = await page.evaluate(
                    """() => ({
                        title: document.title,
                        url: location.href,
                        headings: Array.from(document.querySelectorAll('h1,h2,h3'))
                            .slice(0,40)
                            .map(h => ({level: h.tagName.toLowerCase(),
                                        text: (h.innerText||'').trim().slice(0,200)})),
                        inputs: Array.from(document.querySelectorAll('input,textarea,select'))
                            .slice(0,40)
                            .map(i => ({type: i.type||i.tagName.toLowerCase(),
                                         name: i.name||'',
                                         id: i.id||'',
                                         placeholder: i.placeholder||'',
                                         ariaLabel: i.getAttribute('aria-label')||''})),
                        buttons: Array.from(document.querySelectorAll('button,[role=button],a.button'))
                            .slice(0,40)
                            .map(b => (b.innerText||b.getAttribute('aria-label')||'').trim().slice(0,100))
                            .filter(Boolean)
                    })"""
                )
                return {"ok": True, **data}

            if cmd == "set_viewport":
                w = int(args.get("width", DEFAULT_VIEWPORT[0]))
                h = int(args.get("height", DEFAULT_VIEWPORT[1]))
                await page.set_viewport_size({"width": w, "height": h})
                return {"ok": True, "width": w, "height": h}

            return {"ok": False, "error": f"unknown command: {cmd}"}
        except Exception as e:
            return {"ok": False, "error": str(e), "type": type(e).__name__}


# ---------------------------------------------------------------------------
# Serve entry-point (runs in foreground; usually inside a `wsh run` block)
# ---------------------------------------------------------------------------

async def serve_main(start_url: str, port: int) -> None:
    print(f"[wave-browser] launching headless Chromium…", flush=True)
    session = Session()
    await session.start(start_url)

    async def viewer(_req):
        return web.Response(text=VIEWER_HTML, content_type="text/html")

    async def ws_h(req):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        session.clients.add(ws)
        session.first_client_seen = True
        session.last_activity = time.time()
        try:
            init = {"url": session.latest_url}
            if session.latest_frame:
                init["frame"] = session.latest_frame
            await ws.send_str(json.dumps(init))
            async for _ in ws:
                pass
        finally:
            session.clients.discard(ws)
            session.last_activity = time.time()
        return ws

    async def action_h(req):
        data = await req.json()
        session.last_activity = time.time()
        async with session.action_lock:
            result = await session.do(data.get("cmd", ""), data.get("args", {}))
        session.last_activity = time.time()
        return web.json_response(result)

    stop_event = asyncio.Event()

    async def shutdown_h(_req):
        asyncio.get_event_loop().call_later(0.1, stop_event.set)
        return web.json_response({"ok": True})

    async def lifecycle_monitor():
        """Tie the daemon's life to the Wave VDOM viewer.

        Phase 1: wait up to STARTUP_GRACE seconds for the first WS client.
                 If nobody connects, the block must have failed to open --
                 exit so we don't leak a headless Chromium.
        Phase 2: once at least one viewer has connected, exit when both the
                 viewer and the /action HTTP API have been idle for
                 IDLE_TIMEOUT seconds.
        """
        if IDLE_TIMEOUT <= 0:
            return  # manual-only mode
        deadline = time.time() + STARTUP_GRACE
        while time.time() < deadline and not stop_event.is_set():
            if session.first_client_seen:
                break
            await asyncio.sleep(0.5)
        if not session.first_client_seen:
            sys.stderr.write(
                f"[wave-browser] no viewer connected within {STARTUP_GRACE:.0f}s -- shutting down\n"
            )
            stop_event.set()
            return
        while not stop_event.is_set():
            if session.clients:
                session.last_activity = time.time()
            elif time.time() - session.last_activity > IDLE_TIMEOUT:
                sys.stderr.write(
                    f"[wave-browser] viewer closed and idle {IDLE_TIMEOUT:.0f}s -- shutting down\n"
                )
                stop_event.set()
                return
            await asyncio.sleep(1.0)

    app = web.Application()
    app.router.add_get("/", viewer)
    app.router.add_get("/ws", ws_h)
    app.router.add_post("/action", action_h)
    app.router.add_post("/shutdown", shutdown_h)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    viewer_url = f"http://127.0.0.1:{port}/"
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "url": viewer_url,
                "started_at": time.time(),
            }
        )
    )

    # Open the live screencast as a Wave web/VDOM block. That block is the
    # only Wave-side UI for this session -- closing it severs the WebSocket
    # and the lifecycle monitor below exits the process.
    print(f"[wave-browser] viewer: {viewer_url}", flush=True)
    print(_open_in_wave(viewer_url), flush=True)
    print("[wave-browser] ready -- close the Wave viewer block to stop the browser.", flush=True)

    loop = asyncio.get_event_loop()
    # SIGHUP arrives when Wave closes the parent terminal block (not on Windows).
    _sigs = [signal.SIGTERM, signal.SIGINT]
    _sighup = getattr(signal, "SIGHUP", None)
    if _sighup is not None:
        _sigs.append(_sighup)
    for sig in _sigs:
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows does not support add_signal_handler

    monitor_task = asyncio.create_task(lifecycle_monitor())
    try:
        await stop_event.wait()
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except (asyncio.CancelledError, Exception):
            pass
    print("[wave-browser] stopping…", flush=True)
    await session.stop()
    await runner.cleanup()
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass
    print("[wave-browser] stopped.", flush=True)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _alloc_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check.

    On Windows ``os.kill(pid, 0)`` is implemented via TerminateProcess and
    actually kills the target, so we must use OpenProcess/WaitForSingleObject
    instead. On POSIX, signal 0 is the standard liveness probe.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _session_info() -> Optional[dict]:
    if not SESSION_FILE.exists():
        return None
    try:
        info = json.loads(SESSION_FILE.read_text())
    except Exception:
        return None
    try:
        alive = _pid_alive(int(info["pid"]))
    except (KeyError, ValueError):
        alive = False
    if not alive:
        try:
            SESSION_FILE.unlink()
        except FileNotFoundError:
            pass
        return None
    return info


def _detect_wave() -> bool:
    tp = (os.environ.get("TERM_PROGRAM") or "").lower()
    return bool(
        os.environ.get("WAVETERM")
        or os.environ.get("WAVETERM_VERSION")
        or os.environ.get("WAVE_VERSION")
        or tp in ("waveterm", "wave", "wave-terminal")
    )


_RESOLVED_WSH: Optional[str] = None


def _find_wsh() -> Optional[str]:
    """Locate the `wsh` binary even when PATH does not include it.

    Inside a block spawned via `wsh run`, the shell rc files are not sourced,
    so `~/.local/share/waveterm/bin` (or its snap/macOS equivalent) is missing
    from PATH. We probe a few well-known install paths so the serve process
    can still open the viewer block."""
    global _RESOLVED_WSH
    if _RESOLVED_WSH:
        return _RESOLVED_WSH
    p = shutil.which("wsh")
    if p:
        _RESOLVED_WSH = p
        return p
    candidates: list[Path] = []
    home = Path.home()
    candidates.append(home / ".local/share/waveterm/bin/wsh")
    # Snap (Linux): ~/snap/waveterm/<version>/.local/share/waveterm/bin/wsh
    snap_dir = home / "snap/waveterm"
    if snap_dir.exists():
        cur = snap_dir / "current/.local/share/waveterm/bin/wsh"
        candidates.append(cur)
        try:
            for v in sorted(snap_dir.iterdir(), key=lambda p: p.name, reverse=True):
                candidates.append(v / ".local/share/waveterm/bin/wsh")
        except OSError:
            pass
    # macOS
    candidates.append(Path("/Applications/Wave.app/Contents/Resources/app.asar.unpacked/dist/bin/wsh"))
    candidates.append(Path("/Applications/Wave.app/Contents/Resources/bin/wsh"))
    for c in candidates:
        try:
            if c.exists() and os.access(c, os.X_OK):
                _RESOLVED_WSH = str(c)
                return str(c)
        except OSError:
            continue
    return None


def _set_wsh(path: Optional[str]) -> None:
    global _RESOLVED_WSH
    if path:
        _RESOLVED_WSH = path


def _open_in_wave(url: str) -> str:
    wsh = _find_wsh()
    if not wsh:
        return f"wsh not on PATH -- open this URL in any browser: {url}"
    if not _detect_wave():
        return f"not running under Wave Terminal -- open in browser: {url}"
    try:
        # Wave Terminal: `wsh view <url>` opens a Web Block (Chromium view).
        # On Windows, suppress the console window that Windows would otherwise
        # allocate when a console-less parent (DETACHED_PROCESS daemon) spawns
        # a CLI subprocess.
        run_kwargs = {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 10,
        }
        if sys.platform == "win32":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run([wsh, "view", url], **run_kwargs)
        return f"opened Wave Web Block: {url}"
    except subprocess.CalledProcessError as e:
        return f"wsh view failed ({e.returncode}): {(e.stderr or e.stdout or '').strip()}"
    except Exception as e:
        return f"wsh view error: {e}"


def _post(path: str, body: dict, timeout: float = 120) -> dict:
    info = _session_info()
    if not info:
        return {"ok": False, "error": "no session running -- run `wave-browser start [URL]` first"}
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"daemon unreachable: {e}"}


def _print_result(result: dict) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_start(args) -> int:
    info = _session_info()
    if info:
        msg = _open_in_wave(info["url"])
        print(json.dumps({"ok": True, "already_running": True, "url": info["url"], "wave": msg}, indent=2))
        return 0

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    port = _alloc_port()
    wsh_path = _find_wsh()

    # The serve process runs as a detached helper of THIS shell -- invisible to
    # the user. It opens exactly one Wave block (the web/VDOM viewer at
    # http://127.0.0.1:<port>/) via `wsh view`. When the user closes that
    # block the WebSocket disconnects, the IDLE_TIMEOUT trips, and the
    # subprocess exits -- taking Chromium with it. No terminal block, no
    # zombies.
    script = os.path.abspath(__file__)
    log_fp = open(LOG_FILE, "ab", buffering=0)
    serve_argv = [sys.executable, script, "__serve__", args.url, str(port)]
    if wsh_path:
        serve_argv.append(wsh_path)
    popen_kwargs = {
        "stdout": log_fp,
        "stderr": log_fp,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # Give the daemon its OWN hidden console instead of fully detaching.
        # Rationale:
        # - DETACHED_PROCESS (no console at all) would cause Playwright's
        #   internal Node driver and wsh.exe to trigger a fresh, VISIBLE
        #   console window because they are console apps and Windows
        #   allocates one when the parent has none.
        # - CREATE_NO_WINDOW allocates a hidden console for the daemon. That
        #   hidden console is then inherited by every child subprocess, so
        #   no command-prompt window ever appears.
        # - CREATE_NEW_PROCESS_GROUP detaches the daemon from the parent
        #   shell's Ctrl+C/Ctrl+Break group, so the parent CLI can exit
        #   cleanly without sending signals to the daemon.
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        )
    else:
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(serve_argv, **popen_kwargs)

    deadline = time.time() + 60
    info = None
    while time.time() < deadline:
        info = _session_info()
        if info:
            break
        time.sleep(0.25)

    if not info:
        print(json.dumps({"ok": False, "error": f"timeout starting serve process (see {LOG_FILE})"},
                         indent=2), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "pid": info["pid"],
                "url": info["url"],
                "hint": "Now drive it with `wave-browser goto <url>`, `wave-browser click <selector>`, "
                        "etc. Closing the Wave viewer block stops the browser.",
            },
            indent=2,
        )
    )
    return 0


def cmd_stop(_args) -> int:
    info = _session_info()
    if not info:
        print(json.dumps({"ok": True, "running": False}, indent=2))
        return 0
    _post("/shutdown", {}, timeout=5)
    deadline = time.time() + 10
    while time.time() < deadline and _session_info():
        time.sleep(0.2)
    if _session_info():
        try:
            os.kill(int(info["pid"]), signal.SIGTERM)
        except OSError:
            pass
    print(json.dumps({"ok": True, "stopped": True}, indent=2))
    return 0


def cmd_status(_args) -> int:
    info = _session_info()
    if not info:
        print(json.dumps({"ok": True, "running": False}, indent=2))
        return 0
    state = _post("/action", {"cmd": "url"})
    title = _post("/action", {"cmd": "title"})
    print(
        json.dumps(
            {
                "ok": True,
                "running": True,
                "pid": info["pid"],
                "viewer": info["url"],
                "page_url": state.get("url"),
                "page_title": title.get("title"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_open(_args) -> int:
    """Re-attach the running session's viewer to a Wave Terminal block."""
    info = _session_info()
    if not info:
        print(json.dumps({"ok": False, "error": "no session running"}, indent=2))
        return 1
    msg = _open_in_wave(info["url"])
    print(json.dumps({"ok": True, "url": info["url"], "wave": msg}, indent=2))
    return 0


def _do(cmd: str, **args) -> int:
    return _print_result(_post("/action", {"cmd": cmd, "args": args}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wave-browser", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="start a headless Chromium session and open the Wave VDOM viewer")
    sp.add_argument("url", nargs="?", default="about:blank")
    sp.set_defaults(fn=cmd_start)

    sub.add_parser("stop", help="stop the running session").set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="show running-session info").set_defaults(fn=cmd_status)
    sub.add_parser("open", help="re-open the viewer in a Wave block").set_defaults(fn=cmd_open)

    sp = sub.add_parser("goto", help="navigate the page to URL")
    sp.add_argument("url")
    sp.add_argument("--wait-until", default="domcontentloaded",
                    choices=["load", "domcontentloaded", "networkidle", "commit"])
    sp.add_argument("--timeout", type=int, default=30000)
    sp.set_defaults(fn=lambda a: _do("goto", url=a.url, wait_until=a.wait_until, timeout=a.timeout))

    sp = sub.add_parser("click", help="click the first element matching selector")
    sp.add_argument("selector")
    sp.add_argument("--timeout", type=int, default=10000)
    sp.set_defaults(fn=lambda a: _do("click", selector=a.selector, timeout=a.timeout))

    sp = sub.add_parser("fill", help="fill an input/textarea with value")
    sp.add_argument("selector")
    sp.add_argument("value")
    sp.add_argument("--timeout", type=int, default=10000)
    sp.set_defaults(fn=lambda a: _do("fill", selector=a.selector, value=a.value, timeout=a.timeout))

    sp = sub.add_parser("type", help="type text via keyboard")
    sp.add_argument("text")
    sp.add_argument("--delay", type=int, default=30)
    sp.set_defaults(fn=lambda a: _do("type", text=a.text, delay=a.delay))

    sp = sub.add_parser("press", help="press a single key (e.g. Enter, Tab, ArrowDown)")
    sp.add_argument("key")
    sp.set_defaults(fn=lambda a: _do("press", key=a.key))

    sp = sub.add_parser("wait", help="wait for selector to reach state")
    sp.add_argument("selector")
    sp.add_argument("--state", default="visible",
                    choices=["attached", "detached", "visible", "hidden"])
    sp.add_argument("--timeout", type=int, default=30000)
    sp.set_defaults(fn=lambda a: _do("wait", selector=a.selector, state=a.state, timeout=a.timeout))

    sp = sub.add_parser("sleep", help="wait N seconds")
    sp.add_argument("seconds", type=float)
    sp.set_defaults(fn=lambda a: _do("sleep", seconds=a.seconds))

    sp = sub.add_parser("scroll", help="scroll the page")
    sp.add_argument("direction", choices=["up", "down", "to"])
    sp.add_argument("--amount", type=int, default=600)
    sp.add_argument("--selector", default=None)
    sp.set_defaults(fn=lambda a: _do("scroll", direction=a.direction, amount=a.amount, selector=a.selector))

    sp = sub.add_parser("text", help="return innerText of the first matching element")
    sp.add_argument("selector")
    sp.add_argument("--timeout", type=int, default=10000)
    sp.set_defaults(fn=lambda a: _do("text", selector=a.selector, timeout=a.timeout))

    sp = sub.add_parser("html", help="return the full page HTML (truncated)")
    sp.add_argument("--limit", type=int, default=200000)
    sp.set_defaults(fn=lambda a: _do("html", limit=a.limit))

    sub.add_parser("title", help="get current page title").set_defaults(fn=lambda a: _do("title"))
    sub.add_parser("url", help="get current page URL").set_defaults(fn=lambda a: _do("url"))

    sp = sub.add_parser("eval", help="evaluate a JS expression on the page")
    sp.add_argument("expression")
    sp.set_defaults(fn=lambda a: _do("eval", expression=a.expression))

    sp = sub.add_parser("screenshot", help="save a PNG screenshot")
    sp.add_argument("--path", default=None)
    sp.add_argument("--full-page", action="store_true")
    sp.set_defaults(fn=lambda a: _do("screenshot", path=a.path, full_page=a.full_page))

    sub.add_parser("back", help="history back").set_defaults(fn=lambda a: _do("back"))
    sub.add_parser("forward", help="history forward").set_defaults(fn=lambda a: _do("forward"))
    sub.add_parser("reload", help="reload the page").set_defaults(fn=lambda a: _do("reload"))
    sub.add_parser("links", help="list anchor hrefs on the page").set_defaults(fn=lambda a: _do("links"))
    sub.add_parser("summary", help="structured overview of the page (headings/inputs/buttons)").set_defaults(
        fn=lambda a: _do("summary"))

    sp = sub.add_parser("set-viewport", help="resize the browser viewport")
    sp.add_argument("--width", type=int, default=DEFAULT_VIEWPORT[0])
    sp.add_argument("--height", type=int, default=DEFAULT_VIEWPORT[1])
    sp.set_defaults(fn=lambda a: _do("set_viewport", width=a.width, height=a.height))

    return p


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] in ("__serve__", "__daemon__"):
        url = argv[1] if len(argv) > 1 else "about:blank"
        port = int(argv[2]) if len(argv) > 2 else _alloc_port()
        if len(argv) > 3 and argv[3]:
            _set_wsh(argv[3])
        asyncio.run(serve_main(url, port))
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
