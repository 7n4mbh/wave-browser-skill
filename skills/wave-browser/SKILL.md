---
name: wave-browser
description: Drive a real browser from Wave Terminal. Use this skill whenever the user wants you to visit websites, log in, click through pages, fill forms, scrape content, or run any browser workflow — especially when they are using Wave Terminal and want a live preview of what the browser is doing. The skill launches headless Chromium via Playwright and mirrors the page to a Wave VDOM/preview block using CDP `Page.startScreencast`. No API key is required; you (the agent) drive the browser directly through the CLI documented below.
license: MIT
---

# wave-browser

A self-contained browser-automation skill. You — the agent — operate a headless
Chromium session through small `wave-browser <verb>` commands. The page is
streamed live to a Wave Terminal block so the user can see what is happening.

## When to use this skill

Trigger it any time the user asks you to:

- open a URL, log in somewhere, perform a multi-step web flow
- read text/HTML/data from a page that requires interaction or JS rendering
- fill a form, click a button, navigate tabs, take a screenshot
- "show me the browser" / "do this in a browser" / "go to <site> and ..."

If the user just wants a static page fetched, prefer simple `curl` / `WebFetch`.
Use this skill when you need an actual browser (JS, interaction, login, live view).

## Setup (first time only)

The launcher self-bootstraps a venv on first call. If you want to do it
explicitly:

```bash
bash "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")}/skills/wave-browser/scripts/bootstrap.sh"
```

Within this skill the executable is at:

```
scripts/wave-browser
```

Always invoke it via that wrapper (not the `.py` file).

## Lifecycle

| Step       | Command                              |
| ---------- | ------------------------------------ |
| Start      | `wave-browser start [URL]`           |
| Drive page | any of the action verbs below        |
| Status     | `wave-browser status`                |
| Re-show    | `wave-browser open`                  |
| Stop       | `wave-browser stop`                  |

`start` opens **exactly one Wave block** — a web/VDOM block at
`http://127.0.0.1:<port>/` (via `wsh view`). That block *is* the
custom web app: it shows the live JPEG screencast and is the only Wave-side
UI the user sees. A small Python helper process (Playwright + aiohttp) runs
invisibly in the background to serve frames into that block. There is no
terminal block.

**Lifecycle:** closing the Wave viewer block severs the WebSocket; the
helper process notices and exits after `WAVE_BROWSER_IDLE_TIMEOUT` seconds
(default 10) — headless Chromium goes with it. A busy agent resets the
timer on every `wave-browser <verb>` call, so an active session never gets
shut down out from under it. Set `WAVE_BROWSER_IDLE_TIMEOUT=0` to disable
the safety net (manual `wave-browser stop` only).

If `wsh` / Wave Terminal isn't available, the viewer URL is printed instead
so the user can open it in any browser — same lifecycle rules apply.

Always call `wave-browser stop` explicitly when you finish a task — it is
the fastest, cleanest way to release the browser.

## Action commands

Every action returns JSON. `"ok": true` means success.

### Navigation

```
wave-browser goto <url>            # navigate to a URL
wave-browser back                  # history back
wave-browser forward               # history forward
wave-browser reload                # reload the current page
```

### Inspection (read state back into your context)

```
wave-browser url                   # current URL
wave-browser title                 # current document.title
wave-browser text <selector>       # innerText of first match
wave-browser html [--limit N]      # full page HTML (truncated)
wave-browser links                 # all <a href> on the page
wave-browser summary               # structured digest: headings, inputs, buttons
wave-browser eval "<js>"           # run JS expression, return JSON result
```

`summary` is usually the right first call after `goto` — it gives you an
overview of headings, form fields and clickable elements without dumping the
entire HTML into your context.

### Interaction

```
wave-browser click   <selector>
wave-browser fill    <selector> <value>
wave-browser type    <text>             # types into the focused element
wave-browser press   <key>              # Enter, Tab, ArrowDown, …
wave-browser wait    <selector> [--state visible|hidden|attached|detached] [--timeout ms]
wave-browser sleep   <seconds>
wave-browser scroll  up|down|to [--amount px] [--selector css]
wave-browser screenshot [--path file] [--full-page]
wave-browser set-viewport [--width W] [--height H]
```

Selectors are Playwright selectors: CSS by default, or use `text=...`,
`role=button[name="…"]`, `xpath=...`, etc.

## How to drive a session well

1. **Start with `summary`** after every navigation. Avoid dumping `html` unless
   you really need the markup; it is expensive on context.
2. **Prefer `role=` / `text=` selectors over brittle CSS.** They are stable
   across redesigns.
3. **Wait, don't sleep.** If you need an element, `wave-browser wait <sel>` is
   better than `sleep`.
4. **One action per call.** The user is watching the screencast — small,
   discrete steps make the live view easier to follow and make failures easy
   to diagnose.
5. **Stop the session** when finished, unless the user wants to keep poking at
   the page themselves.

## Example: search and pull one result

```bash
wave-browser start https://duckduckgo.com
wave-browser fill 'input[name="q"]' "wave terminal"
wave-browser press Enter
wave-browser wait 'a[data-testid="result-title-a"]'
wave-browser text 'a[data-testid="result-title-a"]'
wave-browser click 'a[data-testid="result-title-a"]'
wave-browser summary
wave-browser stop
```

## Wave Terminal integration

`wave-browser start` calls `wsh view <viewer-url>` to open a preview/VDOM
block. If the agent is *not* running inside Wave Terminal (no `wsh`, or the
`WAVETERM`/`TERM_PROGRAM` env vars are missing), the URL is printed instead so
the user can open it manually in any browser. The screencast itself works
identically either way.

## Files

```
scripts/wave-browser          # bash launcher (use this)
scripts/wave_browser.py       # daemon + CLI implementation
scripts/bootstrap.sh          # venv setup
requirements.txt              # playwright + aiohttp
```

State lives in `~/.wave-browser/` (override with `$WAVE_BROWSER_HOME`):

```
session.json   # current daemon pid / viewer URL
daemon.log     # daemon stdout/stderr
```
