"""Fully autonomous agent loop — works with both oneshot and persistent browser."""

import asyncio
import base64
import json
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

from .ai import AIClient, ParseError
from .browser import Browser, BrowserBridge


SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "scenarios"

# Cheap gate for the CAPTCHA vision probe — the LLM still makes the call,
# this only decides whether the extra LLM call is worth making at all.
# Brand/product names only: language-neutral.
CAPTCHA_RE = re.compile(
    r"recaptcha|hcaptcha|turnstile|captcha|arkose|funcaptcha|geetest|cf-challenge",
    re.IGNORECASE,
)

RECENT_ACTIONS = 8   # executed actions shown to the LLM each step
MAX_PARSE_FAILURES = 3

# Type for step callback: async fn(step, max_steps, action_text, screenshot_b64)
StepCallback = Callable[[int, int, str, str], Awaitable[None]]

# Type for ask_user callback: async fn(question) -> user response or None (rejected)
AskUserCallback = Callable[[str], Awaitable[str | None]]


async def run_task(
    bridge: BrowserBridge,
    goal: str,
    model: str = "qwen3.5-27b",
    api_base: str = "http://localhost:1234/v1",
    max_steps: int = 30,
    start_url: str | None = None,
    vision_enabled: bool = True,
    temperature: float = 0.3,
    max_tokens: int = 768,
    on_step: StepCallback | None = None,
    on_ask_user: AskUserCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """Run a single task on an existing browser bridge. Returns the result string."""
    ai = AIClient(
        model=model,
        api_base=api_base,
        vision_enabled=vision_enabled,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    # Goal-driven state — the LLM's own plan/progress carried between steps,
    # instead of a replayed transcript of raw actions
    plan = ""
    progress = ""
    notes: list[str] = []          # persistent: user answers — never trimmed
    hint = ""                      # single overwritable slot: stuck/scroll/parse hints
    recent: deque = deque(maxlen=RECENT_ACTIONS)  # executed actions, compact
    last_actions: list[str] = []
    scroll_streak = 0  # consecutive steps that are scroll-only
    parse_failures = 0  # consecutive unparseable LLM responses
    is_zoomed = False  # track CAPTCHA auto-zoom state

    # Create scenario folder
    run_dir = SCENARIOS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(run_dir, exist_ok=True)

    _save_json(run_dir / "task.json", {
        "goal": goal,
        "start_url": start_url,
        "model": model,
        "max_steps": max_steps,
        "started_at": datetime.now().isoformat(),
    })
    print(f"  [scenario] Saving to {run_dir}")

    # Navigate to starting URL if provided, otherwise check if we're on a blank page
    if start_url:
        print(f"  Navigating to {start_url}")
        await bridge.navigate(start_url)
        await asyncio.sleep(1)
    else:
        # Check if browser is on a blank/internal page
        try:
            url_info = await bridge.get_url()
            current_url = url_info.get("url", "") if isinstance(url_info, dict) else ""
            if not current_url or current_url.startswith(("chrome://", "about:", "chrome-extension://")):
                print("  Navigating to https://www.google.com (blank page)")
                await bridge.navigate("https://www.google.com")
                await asyncio.sleep(1)
        except Exception:
            await bridge.navigate("https://www.google.com")
            await asyncio.sleep(1)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    async def _sleep(seconds: float):
        """Sleep that wakes up immediately on cancellation."""
        if cancel_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # Normal — timeout means sleep completed without cancellation

    def _cancel_return(step_num: int) -> str:
        print(f"\n  Task cancelled at step {step_num}")
        _save_json(run_dir / "result.json", {
            "result": "Task cancelled by user", "steps": step_num,
            "finished_at": datetime.now().isoformat(),
        })
        return "Task cancelled by user."

    for step in range(1, max_steps + 1):
        if _cancelled():
            return _cancel_return(step)

        print(f"\n--- Step {step}/{max_steps} ---")
        step_dir = run_dir / f"step_{step:02d}"
        os.makedirs(step_dir, exist_ok=True)

        # 1. Observe — one round trip: numbered outline + badged screenshot
        if _cancelled():
            return _cancel_return(step)

        obs = await _observe(bridge)

        # Auto-zoom for CAPTCHAs. The vision probe is an extra LLM call, so
        # gate it behind a cheap outline check ("or is_zoomed" keeps the
        # un-zoom decision with the LLM once we're zoomed in).
        captcha_detected = False
        if CAPTCHA_RE.search(obs["outline"]) or is_zoomed:
            captcha_detected = await ai.detect_captcha(obs["outline"], obs["image"])
        if captcha_detected and not is_zoomed:
            print("  CAPTCHA detected — zooming to 200%")
            try:
                await bridge.zoom(200)
                await _sleep(0.5)
                obs = await _observe(bridge)  # re-observe at zoomed level
                is_zoomed = True
            except Exception:
                pass
        elif not captcha_detected and is_zoomed:
            print("  CAPTCHA gone — restoring zoom to 100%")
            try:
                await bridge.zoom(100)
                await _sleep(0.3)
                obs = await _observe(bridge)  # re-observe at normal zoom
                is_zoomed = False
            except Exception:
                pass

        dom_text = obs["outline"]
        llm_screenshot = obs["image"]
        labels = {str(k): v for k, v in obs["labels"].items()}
        obs_seq = obs["obsSeq"]

        _save_text(step_dir / "page.txt", dom_text)
        if llm_screenshot:
            _save_image(step_dir / "screenshot.jpg", llm_screenshot)

        # 2. Ask LLM — goal-centric context, race against cancel event
        if _cancelled():
            return _cancel_return(step)
        context = _build_context(goal, plan, progress, notes, hint, recent, dom_text)
        try:
            llm_task = asyncio.create_task(ai.step(context, llm_screenshot))
            if cancel_event:
                cancel_wait = asyncio.create_task(cancel_event.wait())
                done_tasks, pending = await asyncio.wait(
                    [llm_task, cancel_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Fire-and-forget pending tasks — don't await (would block)
                for t in pending:
                    t.cancel()
                    t.add_done_callback(lambda _: None)  # suppress warning
                if cancel_wait in done_tasks:
                    return _cancel_return(step)
                resp = llm_task.result()
            else:
                resp = await llm_task
        except asyncio.CancelledError:
            return _cancel_return(step)
        except ParseError as e:
            parse_failures += 1
            print(f"  Model produced no valid action JSON ({parse_failures}/{MAX_PARSE_FAILURES})")
            _save_json(step_dir / "error.json", {"phase": "parse", "error": str(e)})
            if parse_failures >= MAX_PARSE_FAILURES:
                result = "Aborted: model failed to produce valid actions"
                _save_json(run_dir / "result.json", {
                    "result": result, "error": True, "steps": step,
                    "finished_at": datetime.now().isoformat(),
                })
                return result
            hint = "Your previous reply was not valid JSON. Reply with ONLY the JSON action object."
            continue
        except Exception as e:
            print(f"  LLM error: {e}")
            _save_json(step_dir / "error.json", {"phase": "llm", "error": str(e)})
            await _sleep(2)
            continue

        parse_failures = 0
        hint = ""  # the model saw the previous hint; new ones may be set below
        # Carry plan/progress forward when the model omits them
        plan = resp["plan"] or plan
        progress = resp["progress"] or progress
        actions = resp["actions"]
        if plan:
            print(f"  Plan: {plan[:120]}")
        if progress:
            print(f"  Progress: {progress[:120]}")

        if _cancelled():
            return _cancel_return(step)

        # 3. Execute all actions in sequence
        action_texts = []
        executed_tools: list[str] = []
        for ai_idx, action in enumerate(actions):
            if _cancelled():
                return _cancel_return(step)

            tool_name = action["tool_name"]
            tool_args = action["tool_args"]
            reasoning = action.get("reasoning", "")

            if reasoning and ai_idx == 0:
                print(f"  Thinking: {reasoning[:100]}")
            print(f"  Action {ai_idx + 1}/{len(actions)}: {tool_name}({json.dumps(tool_args)})")

            # Check for "done" — the model declares the goal achieved
            if tool_name == "done":
                result = tool_args.get("result", "")
                print(f"\n  Task complete: {result}")
                _save_json(step_dir / "action.json", {
                    "action": tool_name, "params": tool_args,
                    "reasoning": reasoning, "result": "DONE",
                    "plan": plan, "progress": progress,
                })
                _save_json(run_dir / "result.json", {
                    "result": result, "steps": step,
                    "finished_at": datetime.now().isoformat(),
                })

                if on_step:
                    try:
                        await on_step(step, max_steps, f"✅ Done: {result}", llm_screenshot)
                    except Exception:
                        pass

                return result

            # Handle ask_user — pause for user confirmation/input
            if tool_name == "ask_user":
                question = tool_args.get("question", "Please confirm this action.")
                print(f"  Asking user: {question}")

                _save_json(step_dir / f"action{'_' + str(ai_idx + 1) if len(actions) > 1 else ''}.json", {
                    "action": tool_name, "params": tool_args, "reasoning": reasoning,
                })

                action_texts.append(f"Asking: {question[:60]}")

                user_response = None
                if on_ask_user:
                    # Race ask_user callback against cancel event
                    ask_task = asyncio.create_task(on_ask_user(question))
                    if cancel_event:
                        cancel_wait = asyncio.create_task(cancel_event.wait())
                        done_tasks, pending_tasks = await asyncio.wait(
                            [ask_task, cancel_wait],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending_tasks:
                            t.cancel()
                            t.add_done_callback(lambda _: None)
                        if cancel_wait in done_tasks:
                            return _cancel_return(step)
                        user_response = ask_task.result()
                    else:
                        user_response = await ask_task
                else:
                    # No callback (CLI mode) — print and auto-reject
                    print(f"  No user callback available, skipping ask_user")

                if user_response is None:
                    # User rejected
                    print(f"  User rejected the action")
                    _save_json(run_dir / "result.json", {
                        "result": "Task paused — user declined",
                        "steps": step,
                        "finished_at": datetime.now().isoformat(),
                    })
                    if on_step:
                        try:
                            await on_step(step, max_steps, "⏸️ Task paused — you declined the action", llm_screenshot)
                        except Exception:
                            pass
                    return "Task paused — user declined. Browser stays on current page."

                # User provided a response — keep it in persistent notes
                print(f"  User responded: {user_response[:100]}")
                notes.append(f"User said (re: {question[:60]}): {user_response}")
                # Break batch — re-observe so LLM can act on user's response
                break

            # Execute action
            exec_result = await _execute_tool(bridge, tool_name, tool_args, obs_seq)
            print(f"  Result: {json.dumps(exec_result)[:100]}")

            _save_json(step_dir / f"action{'_' + str(ai_idx + 1) if len(actions) > 1 else ''}.json", {
                "action": tool_name, "params": tool_args,
                "reasoning": reasoning, "result": exec_result,
                "plan": plan, "progress": progress,
            })

            executed_tools.append(tool_name)
            last_actions.append(f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}")
            if len(last_actions) > 6:
                last_actions.pop(0)
            recent.append(
                f"{step}. {tool_name}({_compact_args(tool_args)}) → "
                f"{json.dumps(exec_result)[:120]}"
            )

            action_texts.append(_friendly_action(tool_name, tool_args, labels))

            # If action failed, stop executing remaining actions
            if isinstance(exec_result, dict) and exec_result.get("error"):
                print(f"  Action failed, skipping remaining batch actions")
                break

            # Small delay between batched actions (no delay on last one — we'll re-observe anyway)
            if ai_idx < len(actions) - 1:
                await _sleep(0.15)

        if _cancelled():
            return _cancel_return(step)

        # Stuck detection — runs on actions that actually executed; the hint
        # lands in the single hint slot so the NEXT LLM call sees it
        stuck_hint = _detect_stuck(last_actions)
        if stuck_hint:
            print(f"  Stuck detected — {stuck_hint[:60]}")
            hint = stuck_hint
            last_actions.clear()
            scroll_streak = 0

        # Track scroll-only steps — if agent scrolls too many times without
        # clicking/typing/navigating, it's probably lost
        if executed_tools and all(t in ("scroll", "wait") for t in executed_tools):
            scroll_streak += 1
        else:
            scroll_streak = 0

        if scroll_streak >= 4:
            print("  Scroll loop detected — injecting redirect hint")
            hint = (
                "STOP SCROLLING. You have been scrolling for many steps without "
                "taking any meaningful action. Change strategy NOW: navigate() "
                "directly to a relevant website, go_back() and try a different "
                "query, or click a visible result instead of scrolling past it."
            )
            scroll_streak = 0

        # Notify step callback ONCE per step with a POST-action screenshot
        if on_step and action_texts:
            step_text = " → ".join(action_texts)
            # Take fresh screenshot showing the result of the actions
            post_screenshot = llm_screenshot  # fallback to pre-action
            try:
                ss = await asyncio.wait_for(bridge.screenshot(), timeout=5.0)
                if isinstance(ss, dict) and ss.get("image"):
                    post_screenshot = ss["image"]
            except Exception:
                pass
            try:
                await on_step(step, max_steps, step_text, post_screenshot)
            except Exception:
                pass

    _save_json(run_dir / "result.json", {
        "result": "Max steps reached", "steps": max_steps,
        "finished_at": datetime.now().isoformat(),
    })
    return "Max steps reached without completing the task."


class Agent:
    """Oneshot agent — launches browser, runs task, closes browser."""

    def __init__(
        self,
        goal: str,
        url: str | None = None,
        max_steps: int = 30,
        visible: bool = False,
        model: str = "qwen3.5-27b",
        api_base: str = "http://localhost:1234/v1",
        port: int = 7331,
        vision_enabled: bool = True,
        temperature: float = 0.3,
        max_tokens: int = 768,
    ):
        self.goal = goal
        self.start_url = url
        self.max_steps = max_steps
        self.visible = visible
        self.model = model
        self.api_base = api_base
        self.port = port
        self.vision_enabled = vision_enabled
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def run(self) -> str:
        async with Browser(port=self.port, visible=self.visible) as browser:
            return await run_task(
                bridge=browser.bridge,
                goal=self.goal,
                model=self.model,
                api_base=self.api_base,
                max_steps=self.max_steps,
                start_url=self.start_url,
                vision_enabled=self.vision_enabled,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )


# --- Helpers ---

def _save_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _save_text(path: Path, text: str):
    with open(path, "w") as f:
        f.write(text)


def _save_image(path: Path, b64_data: str):
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64_data))


async def _observe(bridge: BrowserBridge) -> dict:
    """One extension round trip: numbered outline + set-of-marks screenshot.

    Returns a dict with keys: outline, image (b64 JPEG with index badges),
    labels (index -> short name), obsSeq (staleness token for actions).
    """
    try:
        obs = await asyncio.wait_for(bridge.observe(), timeout=25.0)
    except Exception as e:
        print(f"  Observation failed: {e}")
        obs = {"error": str(e)}
    if not isinstance(obs, dict):
        obs = {"error": "malformed observation"}

    if obs.get("error") or not obs.get("outline"):
        return {
            "outline": (
                "Page: (blank or internal page)\n"
                "This is a blank or internal browser page with no interactive "
                "content. Use navigate() to go to a website."
            ),
            "image": "",
            "labels": {},
            "obsSeq": -1,
        }

    obs.setdefault("image", "")
    obs.setdefault("labels", {})
    obs.setdefault("obsSeq", -1)
    return obs


# Element tools → extension act action + tool-arg → act-param mapping.
# All addressed by observation index; executed in one extension round trip.
_ELEMENT_ACTIONS = {
    "click":         ("click",        {"index": "index"}),
    "double_click":  ("double_click", {"index": "index"}),
    "right_click":   ("right_click",  {"index": "index"}),
    "hover":         ("hover",        {"index": "index"}),
    "extract_text":  ("extract_text", {"index": "index"}),
    "type_text":     ("type",         {"index": "index", "text": "text"}),
    "select_option": ("select",       {"index": "index", "value": "value"}),
    "press_key":     ("press_key",    {"index": "index", "key": "key"}),
    "drag_drop":     ("drag_drop",    {"from_index": "index", "to_index": "to_index"}),
}

# Non-element tools → coroutine factory on the bridge
_BRIDGE_ACTIONS = {
    "scroll":     lambda b, a: b.scroll(a.get("direction", "down")),
    "navigate":   lambda b, a: b.navigate(a["url"]),
    "go_back":    lambda b, a: b.go_back(),
    "go_forward": lambda b, a: b.go_forward(),
    "wait":       lambda b, a: b.wait(max(1, min(5, a.get("seconds", 1)))),
    "new_tab":    lambda b, a: b.new_tab(a.get("url")),
    "switch_tab": lambda b, a: b.switch_tab(a["index"]),
    "close_tab":  lambda b, a: b.close_tab(),
    "list_tabs":  lambda b, a: b.get_tabs(),
    "zoom":       lambda b, a: b.zoom(a.get("level", 100)),
}


def _coerce_indices(params: dict) -> bool:
    """Indices must be integers; the LLM occasionally sends strings."""
    for k in ("index", "to_index"):
        if k in params and params[k] is not None:
            try:
                params[k] = int(params[k])
            except (TypeError, ValueError):
                return False
    return True


async def _execute_tool(bridge, tool_name: str, args: dict, obs_seq: int) -> dict:
    """Execute a tool action on the browser."""
    try:
        # scroll with an index scrolls that element's container
        if tool_name == "scroll" and args.get("index") is not None:
            params = {
                "obsSeq": obs_seq,
                "index": args["index"],
                "direction": args.get("direction", "down"),
            }
            if not _coerce_indices(params):
                return {"error": "index must be an integer element index"}
            return await bridge.act("scroll", params)

        if tool_name in _ELEMENT_ACTIONS:
            act_action, param_map = _ELEMENT_ACTIONS[tool_name]
            params = {"obsSeq": obs_seq}
            for tool_key, act_key in param_map.items():
                if tool_key in args:
                    params[act_key] = args[tool_key]
            if not _coerce_indices(params):
                return {"error": "index must be an integer element index"}
            return await bridge.act(act_action, params)

        if tool_name in _BRIDGE_ACTIONS:
            return await _BRIDGE_ACTIONS[tool_name](bridge, args)

        # Unknown tool — report back so the LLM can self-correct
        return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": str(e)}


def _build_context(
    goal: str,
    plan: str,
    progress: str,
    notes: list[str],
    hint: str,
    recent: deque,
    dom_text: str,
) -> str:
    """One goal-centric observation message — replaces transcript history."""
    lines = [f"GOAL: {goal}", ""]
    lines.append(f"PLAN: {plan or '(none yet — write one)'}")
    lines.append(f"PROGRESS: {progress or '(just started)'}")
    if notes or hint:
        lines.append("NOTES:")
        for n in notes:
            lines.append(f"- {n}")
        if hint:
            lines.append(f"- {hint}")
    if recent:
        lines.append("RECENT ACTIONS:")
        for entry in recent:
            lines.append(f"- {entry}")
    lines.append("")
    lines.append(dom_text)
    return "\n".join(lines)


def _compact_args(args: dict) -> str:
    """Compact one-line rendering of tool args for the RECENT ACTIONS list."""
    parts = []
    for k, v in args.items():
        s = v if isinstance(v, str) else json.dumps(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s!r}" if isinstance(v, str) else f"{k}={s}")
    return ", ".join(parts)


def _detect_stuck(last_actions: list[str]) -> str | None:
    """Detect stuck patterns in recent actions. Returns a hint message or None."""
    if len(last_actions) < 3:
        return None

    # Pattern 1: 3+ identical actions in a row
    if len(last_actions) >= 3 and len(set(last_actions[-3:])) == 1:
        return (
            "You are repeating the exact same action. STOP and try something different. "
            "Navigate to a different URL, click a different element, or use go_back()."
        )

    # Pattern 2: oscillating between 2 actions (e.g. scroll up/down, click A / click B)
    if len(last_actions) >= 4:
        recent4 = last_actions[-4:]
        if recent4[0] == recent4[2] and recent4[1] == recent4[3] and recent4[0] != recent4[1]:
            return (
                "You are going back and forth between two actions in a loop. STOP. "
                "This approach is not working. Try a completely different strategy:\n"
                "- navigate() directly to the website you need\n"
                "- Use a different search query\n"
                "- Click on a result you haven't tried yet"
            )

    # Pattern 3: mostly scrolls in recent actions (4+ out of 6 are scroll)
    if len(last_actions) >= 5:
        scroll_count = sum(1 for a in last_actions[-5:] if a.startswith("scroll:"))
        if scroll_count >= 4:
            return (
                "You have been scrolling excessively without taking action. "
                "The information you need might not be on this page. "
                "Try navigating directly to a relevant website, or click on "
                "something visible instead of scrolling past it."
            )

    return None


def _element_label(args: dict, labels: dict, key: str = "index") -> str:
    """Human-readable name of an element by its observation index."""
    idx = args.get(key)
    if idx is None:
        return "element"
    name = labels.get(str(idx), "")
    return f'"{name}"' if name else f"[{idx}]"


def _friendly_action(tool_name: str, args: dict, labels: dict) -> str:
    """Format a tool action as a nice user-friendly description with emojis."""
    label = _element_label(args, labels)
    if tool_name == "click":
        return f"👆 Clicking {label}"
    elif tool_name == "double_click":
        return f"👆👆 Double-clicking {label}"
    elif tool_name == "right_click":
        return f"🖱️ Right-clicking {label}"
    elif tool_name == "type_text":
        text = args.get("text", "")
        if len(text) > 30:
            text = text[:27] + "..."
        return f'⌨️ Typing "{text}" into {label}'
    elif tool_name == "press_key":
        key = args.get("key", "")
        if args.get("index") is not None:
            return f"⌨️ Pressing {key} on {label}"
        return f"⌨️ Pressing {key}"
    elif tool_name == "select_option":
        return f"📋 Selecting \"{args.get('value', '')}\" in {label}"
    elif tool_name == "hover":
        return f"🔍 Hovering over {label}"
    elif tool_name == "navigate":
        url = args.get("url", "")
        return f"🌐 Going to {url[:50]}"
    elif tool_name == "go_back":
        return "⬅️ Going back"
    elif tool_name == "go_forward":
        return "➡️ Going forward"
    elif tool_name == "scroll":
        d = args.get("direction", "down")
        return "⬇️ Scrolling down" if d == "down" else "⬆️ Scrolling up"
    elif tool_name == "wait":
        return "⏳ Waiting for page to load..."
    elif tool_name == "extract_text":
        return f"📖 Reading {label}"
    elif tool_name == "new_tab":
        url = args.get("url", "")
        return f"🆕 Opening new tab{': ' + url[:40] if url else ''}"
    elif tool_name == "switch_tab":
        return f"🔀 Switching to tab {args.get('index', 0)}"
    elif tool_name == "close_tab":
        return "❌ Closing tab"
    elif tool_name == "list_tabs":
        return "📑 Checking open tabs"
    elif tool_name == "drag_drop":
        return f"🔄 Dragging {_element_label(args, labels, 'from_index')} to {_element_label(args, labels, 'to_index')}"
    elif tool_name == "zoom":
        level = args.get("level", 100)
        return f"🔎 Zooming to {level}%"
    elif tool_name == "ask_user":
        q = args.get("question", "")
        if len(q) > 60:
            q = q[:57] + "..."
        return f"💬 {q}"
    else:
        return f"⚙️ {tool_name}"
