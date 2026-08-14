# 👻 Ghost Browser Agent

**Undetectable AI-powered browser automation.** Controls a real Chrome browser through a Chrome Extension + WebSocket bridge — no CDP, no WebDriver, no Selenium. Fundamentally invisible to anti-bot detection.

> 🤖 Give it a task in plain language, and it browses the web like a human — it *looks* at the page, sees numbered elements, and points at what it wants. Works on pages in **any language**.

---

## ✨ Features

- 🕵️ **Truly undetectable** — No automation protocols. Real Chrome, real fingerprints, invisible to anti-bot systems
- 👁️ **Human-like perception (set-of-marks)** — Every interactive element gets a number, shown in a text outline **and** as red badges on the screenshot; the AI acts by number, never by guessing CSS selectors
- 🌍 **Multi-language by design** — Page text passes to the AI verbatim in the page's own language; zero text matching in code, answers in the language of your task
- 🧭 **Goal-driven loop** — The AI maintains an explicit plan + progress each step and stops when the goal is achieved (`max_steps` is just a safety cap)
- 🖼️ **Frames & shadow DOM included** — Consent dialogs in cross-origin iframes and web-component UIs are enumerated like everything else
- ⚡ **One round trip per observation and per action** — outline + badged screenshot arrive together; acting targets the element's own frame directly
- 🛡️ **Human-in-the-loop** — Asks for confirmation before passwords, payments, or sensitive actions
- 💬 **Telegram integration** — Control the agent from your phone with live screenshots
- 🔄 **Smart tab management** — Opens, switches, and compares across multiple tabs
- 📊 **Scenario logging** — Every task is recorded with the exact outline, badged screenshot, and action history the AI saw
- 🖥️ **Cross-platform** — macOS, Linux, and Windows

---

## 🏗️ Architecture

```
┌─────────────┐    WebSocket     ┌──────────────────┐     chrome.scripting     ┌──────────┐
│  Python CLI  │◄──────────────►│  Ghost Extension  │◄────────────────────────►│  Web Page │
│  + AI Agent  │  localhost:7331 │  (background.js)  │    executeScript         │  (DOM)   │
└──────┬───────┘                 └──────────────────┘    (isolated world,       └──────────┘
       │                                                  all frames)
       │ OpenAI-compatible API
       ▼
┌──────────────┐
│   LLM Server │
│  (LMStudio)  │
└──────────────┘
```

### 👁️ How the agent perceives a page

Each step is **one `observe` round trip**. The extension walks the visible viewport of *every frame* (including cross-origin iframes and open shadow roots), detects interactive elements by **behavior** — tags, ARIA roles, click handlers, `tabindex`, pointer cursor — and returns:

1. A **numbered text outline** with page text verbatim, in whatever language the page uses:

```
Page: Google
URL: https://www.google.com/
Scroll: 0% (more content below)
Interactive elements are numbered [N]; the same numbers appear as red badges on the screenshot.

[2]<textarea name=q placeholder=Search></textarea>
[3]<button>Google Search</button>
--- frame: https://consent.google.com/… ---
Bevor Sie zu Google weitergehen
[12]<div role=button>Alle akzeptieren</div>
```

2. A **screenshot with matching red number badges** painted on it, so vision and structure agree on what `[12]` is.

The AI then acts by index — `click(12)` — and the extension executes on the **live element reference** in that element's own frame. If the page changed since the observation, the action fails loudly with *"stale — re-observe"* instead of ever clicking the wrong thing.

### 🔑 Why it's undetectable

| Traditional tools | Ghost Agent |
|---|---|
| ❌ CDP / WebDriver protocols | ✅ Chrome Extension + WebSocket |
| ❌ `navigator.webdriver = true` | ✅ No automation flags |
| ❌ Detectable headless mode | ✅ Real Chrome binary |
| ❌ Synthetic fingerprints | ✅ Real TLS, HTTP/2, WebGL, Canvas |
| ❌ Content scripts visible to page | ✅ Isolated world injection — page JS can't see it |

---

## 📋 Requirements

- 🐍 Python 3.11+
- 📦 [uv](https://docs.astral.sh/uv/) (package manager)
- 🤖 An OpenAI-compatible LLM server (e.g. [LM Studio](https://lmstudio.ai/)) running a **vision** model

> 💡 Chrome for Testing is **auto-downloaded** on first run — no manual Chrome install needed.

---

## 🚀 Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Edit config.yml with your LLM settings

# 3. Run a task!
uv run python cli.py run "search Google for weather in Paris and tell me the temperature"
```

That's it! The agent launches Chrome, navigates, interacts with the page, and returns the answer.

---

## ⚙️ Configuration

All settings live in `config.yml`:

```yaml
telegram:
  bot_token: ""              # 🤖 Get from @BotFather on Telegram
  allowed_users: []          # 🔒 User IDs allowed to use the bot (empty = allow all)

llm:
  model: "qwen/qwen3.8-27b"  # 🧠 Model name in your LLM server (vision-capable)
  api_base: "http://localhost:1234/v1"
  vision_enabled: true       # 👁️ Send badged screenshots to LLM
  max_tokens: 4096           # 💭 Thinking models spend reasoning tokens from this budget
  temperature: 0.3

browser:
  ws_port: 7331              # 🔌 WebSocket port for extension bridge
  visible: false             # 🖥️ Show browser window

agent:
  max_steps: 50              # 🔄 Safety cap — the agent stops itself when the goal is done
```

> ⚠️ `config.yml` may contain secrets (bot tokens). It's in `.gitignore` by default.

---

## 💻 CLI Usage

### 🎯 Oneshot Mode

Run a single task, then close the browser:

```bash
# Simple query
uv run python cli.py run "search Google for weather in Paris and tell me the temperature"

# Start on a specific URL
uv run python cli.py run "find the cheapest laptop" --url https://amazon.com

# Show the browser while it works
uv run python cli.py run "go to github.com and star the first trending repo" --visible
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--url` | Starting URL | LLM decides |
| `--visible` | Show browser window | Hidden |
| `--max-steps` | Max agent steps | 30 |
| `--model` | Override LLM model | From config |
| `--api-base` | Override LLM endpoint | From config |
| `--port` | WebSocket port | 7331 |

### 🔄 Persistent Daemon Mode

Keep the browser open and send tasks from another terminal:

```bash
# Terminal 1: Start the daemon
uv run python cli.py start

# Terminal 2: Send tasks
uv run python cli.py task "search for latest AI news"
uv run python cli.py task "open twitter and check trending topics"

# Check status or stop
uv run python cli.py status
uv run python cli.py stop
```

### 💬 Telegram Bot Mode

Control the agent from your phone — get live screenshots of what the agent is doing!

```bash
# Set bot_token in config.yml, then:
uv run python cli.py telegram

# Or pass token directly:
uv run python cli.py telegram --token "YOUR_BOT_TOKEN"
```

**Telegram commands:**

| Command | Description |
|---------|-------------|
| _Any text message_ | 🎯 Runs it as a browser task |
| `/screenshot` | 📸 Get current page screenshot |
| `/stop` | 🛑 Cancel the running task |
| `/reject` | ❌ Decline agent's question and pause |
| `/help` | ❓ Show available commands |

**How it works:**
1. Send a task like _"find the best rated book on Amazon under $20"_
2. The agent browses autonomously, sending you screenshots at each step
3. When it needs your input (passwords, payment, choices), it asks via Telegram
4. Reply with your answer to continue, or `/reject` to decline

---

## 🤖 Agent Actions

All element actions address elements by their **observation index** `[N]`:

| Action | Description |
|--------|-------------|
| 👆 `click(index)` | Click element [N] |
| 👆👆 `double_click(index)` | Double-click element [N] |
| 🖱️ `right_click(index)` | Right-click element [N] |
| ⌨️ `type_text(index, text)` | Clear field [N] and type text (Unicode-safe, any script) |
| ⌨️ `press_key(key, index?)` | Press a key (Enter, Escape, Tab, …), globally or on [N] |
| 📋 `select_option(index, value)` | Pick an option by value or its exact visible label |
| 🔍 `hover(index)` | Hover over element [N] |
| 🔄 `drag_drop(from_index, to_index)` | Drag [A] onto [B] |
| 📖 `extract_text(index)` | Read element [N]'s full text |
| ⬇️ `scroll(direction, index?)` | Scroll the page, or element [N]'s own scrollable container |
| 🌐 `navigate(url)` | Go to a URL |
| ⬅️ `go_back()` / ➡️ `go_forward()` | Browser navigation |
| 🆕 `new_tab(url)` | Open a new tab |
| 🔀 `switch_tab(index)` | Switch between tabs |
| ❌ `close_tab()` | Close current tab |
| 📑 `list_tabs()` | List all open tabs |
| ⏳ `wait(seconds)` | Wait for page to load |
| 🔎 `zoom(level)` | Set page zoom % |
| 💬 `ask_user(question)` | Ask the user for input |
| ✅ `done(result)` | Goal achieved — return the answer |

> 🔒 Indices are valid only for the current observation. If the page changes between seeing and acting, the action fails with a clean *"re-observe"* error and the agent picks again from fresh numbers — it never clicks blindly.

---

## 🧠 How the Agent Thinks

The loop is **goal-driven, not step-driven**. Every response from the AI carries its own state:

```json
{"plan": "1. dismiss consent [done] 2. search [now] 3. read result 4. answer",
 "progress": "consent accepted, search box visible",
 "actions": [{"action": "type_text", "params": {"index": 2, "text": "weather in Paris"}},
             {"action": "press_key", "params": {"key": "Enter"}}]}
```

1. 🎯 **Plan** — numbered steps toward the goal, updated with `[done]` / `[now]` markers each step
2. 📍 **Progress** — one sentence on where it is right now
3. ✅ **Self-check** — before acting, it compares progress against the goal and calls `done(result)` the moment the goal is achieved; `max_steps` is only a safety cap
4. ⚡ **Batching** — predictable sequences (type + Enter, multi-field forms) run without re-observing; anything uncertain runs one action at a time

For research and shopping tasks, the agent gathers first, compares 3–5 options (ratings, reviews, price), and presents findings before any purchase.

---

## 🛡️ Safety — Human-in-the-Loop

The agent **always asks for your confirmation** before:

- 🔐 Entering passwords or credentials
- 💳 Submitting payments or financial transactions
- 📋 Filling personal information (SSN, credit card, address)
- 🗑️ Deleting accounts or data
- 🔒 Changing security settings (2FA, email, password)
- 🛒 Confirming purchases or subscriptions
- ⚠️ Any irreversible action

You can approve, provide information, or reject (task pauses, browser stays on current page).

---

## 📁 Project Structure

```
browser-agent/
├── 📄 config.yml                  # Configuration (LLM, Telegram, browser)
├── 📄 cli.py                      # CLI entry point
├── 📁 extension/
│   ├── 📄 manifest.json           # Chrome MV3 extension manifest
│   └── 📄 background.js           # WebSocket bridge + observe (outline & badges) + act-by-index
├── 📁 src/browser_agent/
│   ├── 📄 agent.py                # Goal-driven agent loop + scenario logging
│   ├── 📄 ai.py                   # LLM client (plan/progress JSON dispatch + vision)
│   ├── 📄 browser.py              # Chrome launcher + WebSocket server bridge
│   ├── 📄 config.py               # YAML config loader
│   ├── 📄 daemon.py               # Persistent browser daemon (TCP commands)
│   └── 📄 telegram_bot.py         # Telegram bot interface
└── 📁 scenarios/                  # Auto-saved task logs
```

---

## 📊 Scenario Logging

Every task run is automatically saved to `scenarios/<timestamp>/`:

```
scenarios/20260814_214423/
├── task.json              # 🎯 Goal, model, config
├── step_01/
│   ├── page.txt           # 📄 Numbered outline exactly as the LLM saw it
│   ├── screenshot.jpg     # 📸 Screenshot with the red [N] badges
│   └── action.json        # 🤖 LLM decision + plan/progress + execution result
├── step_02/
│   └── ...
└── result.json            # ✅ Final result
```

Great for debugging, replaying, and understanding agent behavior.

---

## 🖥️ Platform Support

| Platform | Status |
|----------|--------|
| 🍎 macOS (Apple Silicon) | ✅ Fully supported |
| 🍎 macOS (Intel) | ✅ Fully supported |
| 🐧 Linux (x64) | ✅ Fully supported (`xvfb-run` for headless) |
| 🪟 Windows (x64) | ✅ Supported |

---

## 📜 License

MIT
