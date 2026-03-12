# 👻 Ghost Browser Agent

**Undetectable AI-powered browser automation.** Controls a real Chrome browser through a Chrome Extension + WebSocket bridge — no CDP, no WebDriver, no Selenium. Fundamentally invisible to anti-bot detection.

> 🤖 Give it a task in plain English, and it browses the web like a human — clicking, typing, scrolling, and reasoning its way to the answer.

---

## ✨ Features

- 🕵️ **Truly undetectable** — No automation protocols. Real Chrome, real fingerprints, invisible to anti-bot systems
- 🧠 **AI-powered reasoning** — LLM sees both a screenshot and structured DOM, thinks step-by-step
- 👁️ **Vision + DOM understanding** — Combines visual and structural page analysis
- ⚡ **Multi-action batching** — Executes predictable action sequences without re-observing (e.g. type + Enter)
- 🛡️ **Human-in-the-loop** — Asks for confirmation before passwords, payments, or sensitive actions
- 💬 **Telegram integration** — Control the agent from your phone with live screenshots
- 🔄 **Smart tab management** — Opens, switches, and compares across multiple tabs
- 📊 **Scenario logging** — Every task is recorded with screenshots, DOM, and action history
- 🖥️ **Cross-platform** — macOS, Linux, and Windows

---

## 🏗️ Architecture

```
┌─────────────┐    WebSocket     ┌──────────────────┐     chrome.scripting     ┌──────────┐
│  Python CLI  │◄──────────────►│  Ghost Extension  │◄────────────────────────►│  Web Page │
│  + AI Agent  │  localhost:7331 │  (background.js)  │    executeScript         │  (DOM)   │
└──────┬───────┘                 └──────────────────┘    (isolated world)       └──────────┘
       │
       │ OpenAI-compatible API
       ▼
┌──────────────┐
│   LLM Server │
│  (LMStudio)  │
└──────────────┘
```

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
- 🤖 An OpenAI-compatible LLM server (e.g. [LM Studio](https://lmstudio.ai/))

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
  model: "qwen3.5-9b-mlx"   # 🧠 Model name in your LLM server
  api_base: "http://localhost:1234/v1"
  vision_enabled: true       # 👁️ Send screenshots to LLM
  max_tokens: 2048
  temperature: 0.3

browser:
  ws_port: 7331              # 🔌 WebSocket port for extension bridge
  visible: false             # 🖥️ Show browser window

agent:
  max_steps: 50              # 🔄 Max actions per task
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

The AI agent can perform these actions on the browser:

| Action | Description |
|--------|-------------|
| 👆 `click(id)` | Click an element |
| 👆👆 `double_click(id)` | Double-click an element |
| 🖱️ `right_click(id)` | Right-click an element |
| ⌨️ `type_text(id, text)` | Clear field and type text |
| ⌨️ `press_key(key)` | Press a key (Enter, Escape, Tab, etc.) |
| 📋 `select_option(id, value)` | Choose a dropdown option |
| 🔍 `hover(id)` | Hover over an element |
| 🔄 `drag_drop(from, to)` | Drag and drop |
| 🌐 `navigate(url)` | Go to a URL |
| ⬅️ `go_back()` / ➡️ `go_forward()` | Browser navigation |
| ⬇️ `scroll(direction)` | Scroll up or down |
| 🆕 `new_tab(url)` | Open a new tab |
| 🔀 `switch_tab(index)` | Switch between tabs |
| ❌ `close_tab()` | Close current tab |
| 📑 `list_tabs()` | List all open tabs |
| ⏳ `wait(seconds)` | Wait for page to load |
| 📖 `extract_text(id)` | Read element's full text |
| 💬 `ask_user(question)` | Ask the user for input |
| ✅ `done(result)` | Task complete — return answer |

---

## 🧠 How the Agent Thinks

The agent doesn't just blindly click — it **reasons** about each step:

1. 🎯 **Understand the goal** — What does the user really want?
2. 📝 **Plan the approach** — Break complex tasks into steps
3. 📍 **Track progress** — Where am I in the plan?
4. 🤔 **Choose the best action** — Not just any action, the smartest one

For research and shopping tasks, the agent:
- 🔍 **Gathers information** before making decisions
- ⚖️ **Compares multiple options** — doesn't pick the first result
- ⭐ **Considers ratings, reviews, and price** together
- 💬 **Presents findings** to you before making purchases

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
│   └── 📄 background.js           # WebSocket bridge + DOM extraction + commands
├── 📁 src/browser_agent/
│   ├── 📄 agent.py                # Autonomous agent loop + scenario logging
│   ├── 📄 ai.py                   # LLM client (JSON tool dispatch + vision)
│   ├── 📄 browser.py              # Chrome launcher + WebSocket server bridge
│   ├── 📄 config.py               # YAML config loader
│   ├── 📄 daemon.py               # Persistent browser daemon (TCP commands)
│   ├── 📄 dom.py                  # DOM state formatting for LLM
│   ├── 📄 screenshot.py           # Screenshot decode + resize (JPEG for LLM, PNG for logs)
│   └── 📄 telegram_bot.py         # Telegram bot interface
└── 📁 scenarios/                  # Auto-saved task logs
```

---

## 📊 Scenario Logging

Every task run is automatically saved to `scenarios/<timestamp>/`:

```
scenarios/20260312_143052/
├── task.json              # 🎯 Goal, model, config
├── step_01/
│   ├── dom.html           # 📄 DOM sent to LLM
│   ├── screenshot.png     # 📸 Full-size page screenshot
│   └── action.json        # 🤖 LLM decision + execution result
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
