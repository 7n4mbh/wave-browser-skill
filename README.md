# wave-browser

A Wave Terminal **skill** that lets your AI agent (Claude Code, Codex CLI,
Gemini CLI, …) drive a real Chromium browser through Playwright while you
watch the page **live inside Wave Terminal** as a VDOM/preview block.

```
┌──────────────────────────────────────────────────────────────┐
│  ● wave-browser    https://example.com                LIVE   │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│           [ JPEG frames streamed via CDP screencast,         │
│             rendered in a Wave Terminal block ]              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

- **You talk to the agent in natural language.** ("Open arxiv, search for
  RLHF, open the top result, summarise the abstract.")
- **The agent drives the browser directly** via `wave-browser <verb>` calls.
  No external LLM API key is required — *your* agent is the brain.
- **Playwright runs headless**, but a CDP `Page.startScreencast` stream is
  piped to a tiny WebSocket viewer that opens in a Wave Terminal block via
  `wsh view`. The window you see is Wave's own block, not a browser window.

---

## Install

The skill is published in a format that works with every major agent
marketplace and with [skills.sh](https://www.skills.sh/).

### Skills.sh (recommended, works for any agent)

```bash
npx skills add https://github.com/7n4mbh/wave-browser-skill --skill wave-browser
```

### Claude Code

```text
/plugin install 7n4mbh/wave-browser-skill
```

### Codex CLI

```bash
codex plugin marketplace add 7n4mbh/wave-browser-skill
```

### Gemini CLI

```bash
gemini extensions install https://github.com/7n4mbh/wave-browser-skill
```

### Manual / npm

```bash
git clone https://github.com/7n4mbh/wave-browser-skill ~/.skills/wave-browser-skill
bash ~/.skills/wave-browser-skill/skills/wave-browser/scripts/bootstrap.sh
```

The first call to `wave-browser start` auto-creates a local Python venv and
installs Playwright + Chromium under the skill directory. No system-wide
changes are made.

### Requirements

- macOS or Linux (Wave Terminal's supported platforms)
- Python ≥ 3.10
- [Wave Terminal](https://www.waveterm.dev/) with `wsh` on your `$PATH`
  *(optional — without Wave the viewer URL is just printed)*

---

## Usage

In any supported CLI, after installing, just talk:

> *"Open en.wikipedia.org, search for Wave Terminal, click the first
> result, and tell me the article's opening paragraph."*

The agent picks up the `wave-browser` skill, runs:

```bash
wave-browser start https://en.wikipedia.org
wave-browser fill 'input[name="search"]' "Wave Terminal"
wave-browser press Enter
wave-browser wait '#firstHeading'
wave-browser text '#mw-content-text p:nth-of-type(1)'
wave-browser stop
```

…and the Wave block shows every step live.

### Manual CLI use

You can also drive `wave-browser` yourself from the shell — it is a normal CLI:

```bash
wave-browser start https://news.ycombinator.com
wave-browser summary
wave-browser click 'a.titleline > a'
wave-browser url
wave-browser screenshot --path /tmp/hn.png
wave-browser stop
```

Full command reference is in [`skills/wave-browser/SKILL.md`](skills/wave-browser/SKILL.md).

---

## How it works

```
                 ┌────────────────────────────────────────────────┐
                 │  Wave Terminal                                 │
                 │                                                │
                 │   ┌──────────────────────────────────────┐     │
                 │   │ Wave web/VDOM block (wsh view URL)   │     │
                 │   │ ┌──────────────────────────────────┐ │     │
                 │   │ │ custom HTML viewer               │ │     │
                 │   │ │   <img src=…> ←— JPEG frames ←—  │ │     │
   wave-browser  │   │ │   over WebSocket                 │ │     │
   start <url> ──┼─▶ │ └──────────────────────────────────┘ │     │
                 │   └──────────────▲───────────────────────┘     │
                 │                  │ http (localhost)            │
                 │   ┌──────────────┴─────────────────────────┐   │
                 │   │  invisible helper process              │   │
                 │   │  ┌─────────────┐  CDP screencast       │   │
                 │   │  │ Playwright  │ ─────────────────┐    │   │
                 │   │  │  + aiohttp  │                  ▼    │   │
                 │   │  └─────────────┘            ┌─────────┐│   │
                 │   │                             │Chromium ││   │
                 │   └─────────────────────────────│headless ││   │
                 │                                 └─────────┘│   │
                 │   wave-browser goto/click/fill/… ────▶ HTTP/action
                 └────────────────────────────────────────────────┘
```

- `wave-browser start <url>` spawns a background helper process (no terminal
  block).
- The helper opens **one** Wave block — a web/VDOM block backed by our HTML
  viewer — via `wsh view http://127.0.0.1:<port>/`.
- That block streams the headless-Chromium screencast in via WebSocket. To
  the user, *this single block is the app*.
- Subsequent `wave-browser <verb>` calls (run by the agent in its own Claude
  Code / Codex / Gemini block) POST to `/action` on localhost, so the browser
  state survives across separate agent tool calls.

### Lifecycle: no zombies, ever

| User does…                | What happens                                |
| ------------------------- | ------------------------------------------- |
| **Closes the viewer block**| WebSocket disconnects; with no `/action` activity for ~10 s the helper self-exits and Chromium is gone. |
| **Closes the whole tab**  | Same as above — block disappears, helper exits. |
| **`wave-browser stop`**   | Helper exits cleanly. |
| **No viewer ever opens**  | After `WAVE_BROWSER_STARTUP_GRACE` seconds (default 60), helper gives up and exits. |

A busy agent resets the idle timer on every `wave-browser <verb>` call, so
an active session is never shut down out from under it.

Tune the safety net with environment variables:

```bash
export WAVE_BROWSER_STARTUP_GRACE=60   # seconds to wait for first WS connection
export WAVE_BROWSER_IDLE_TIMEOUT=10    # idle seconds before auto-exit; 0 disables
```

---

## Files

```
.
├── README.md                              # this file
├── .claude-plugin/plugin.json             # Claude Code plugin manifest
├── gemini-extension.json                  # Gemini CLI extension manifest
├── codex-plugin.json                      # Codex CLI plugin manifest
├── package.json                           # npm / skills.sh metadata
└── skills/
    └── wave-browser/
        ├── SKILL.md                       # agent-facing manual
        ├── requirements.txt
        └── scripts/
            ├── wave-browser               # bash launcher
            ├── bootstrap.sh               # venv setup
            └── wave_browser.py            # daemon + CLI
```

## Privacy & security

- The browser runs **on your machine only**. Nothing is sent to any
  third-party service by this skill.
- The viewer HTTP server binds to `127.0.0.1` (localhost) on a random port.
- Cookies, cache and download artefacts live under `~/.wave-browser/` (or
  `$WAVE_BROWSER_HOME`) and are wiped when you `wave-browser stop`.

## License

MIT
