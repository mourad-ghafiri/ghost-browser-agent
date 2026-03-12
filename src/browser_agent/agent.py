"""Fully autonomous agent loop — works with both oneshot and persistent browser."""

import asyncio
import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable

from .ai import AIClient
from .browser import Browser, BrowserBridge
from .dom import DOMState
from .screenshot import process_screenshot


MAX_HISTORY = 10
SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "scenarios"

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
    max_tokens: int = 512,
    on_step: StepCallback | None = None,
    on_ask_user: AskUserCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    user_queue: asyncio.Queue | None = None,
) -> str:
    """Run a single task on an existing browser bridge. Returns the result string."""
    ai = AIClient(
        model=model,
        api_base=api_base,
        vision_enabled=vision_enabled,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    history: list[dict] = []
    last_actions: list[str] = []
    scroll_streak = 0  # consecutive steps that are scroll-only
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

        # 1. Extract DOM + Screenshot in PARALLEL
        if _cancelled():
            return _cancel_return(step)

        dom_text, llm_screenshot, full_screenshot = await _observe(bridge)

        # Auto-zoom for CAPTCHAs — ask LLM if it sees a CAPTCHA
        captcha_detected = await ai.detect_captcha(dom_text, llm_screenshot)
        if captcha_detected and not is_zoomed:
            print("  CAPTCHA detected — zooming to 200%")
            try:
                await bridge.zoom(200)
                await _sleep(0.5)
                # Re-observe at zoomed level
                dom_text, llm_screenshot, full_screenshot = await _observe(bridge)
                is_zoomed = True
            except Exception:
                pass
        elif not captcha_detected and is_zoomed:
            print("  CAPTCHA gone — restoring zoom to 100%")
            try:
                await bridge.zoom(100)
                await _sleep(0.3)
                # Re-observe at normal zoom
                dom_text, llm_screenshot, full_screenshot = await _observe(bridge)
                is_zoomed = False
            except Exception:
                pass

        _save_text(step_dir / "dom.html", dom_text)
        if full_screenshot:
            _save_image(step_dir / "screenshot.png", full_screenshot)

        # 2. Ask LLM — returns list of actions (race against cancel event)
        if _cancelled():
            return _cancel_return(step)
        trimmed = _trim_history(history)
        try:
            llm_task = asyncio.create_task(ai.step(goal, dom_text, llm_screenshot, trimmed))
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
                actions = llm_task.result()
            else:
                actions = await llm_task
        except asyncio.CancelledError:
            return _cancel_return(step)
        except Exception as e:
            print(f"  LLM error: {e}")
            _save_json(step_dir / "error.json", {"phase": "llm", "error": str(e)})
            await _sleep(2)
            continue

        if _cancelled():
            return _cancel_return(step)

        # 3. Execute all actions in sequence
        action_texts = []
        for ai_idx, action in enumerate(actions):
            if _cancelled():
                return _cancel_return(step)

            tool_name = action["tool_name"]
            tool_args = action["tool_args"]
            reasoning = action.get("reasoning", "")

            if reasoning and ai_idx == 0:
                print(f"  Thinking: {reasoning[:100]}")
            print(f"  Action {ai_idx + 1}/{len(actions)}: {tool_name}({json.dumps(tool_args)})")

            # Check for "done"
            if tool_name == "done":
                result = tool_args.get("result", "")
                print(f"\n  Task complete: {result}")
                _save_json(step_dir / "action.json", {
                    "action": tool_name, "params": tool_args,
                    "reasoning": reasoning, "result": "DONE",
                })
                _save_json(run_dir / "result.json", {
                    "result": result, "steps": step,
                    "finished_at": datetime.now().isoformat(),
                })

                if on_step:
                    try:
                        await on_step(step, max_steps, f"✅ Done: {result}", full_screenshot)
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
                    history.append({
                        "role": "user",
                        "content": f"You asked: {question}\nUser REJECTED this action. Task is paused.",
                    })
                    _save_json(run_dir / "result.json", {
                        "result": "Task paused — user declined",
                        "steps": step,
                        "finished_at": datetime.now().isoformat(),
                    })
                    if on_step:
                        try:
                            await on_step(step, max_steps, "⏸️ Task paused — you declined the action", full_screenshot)
                        except Exception:
                            pass
                    return "Task paused — user declined. Browser stays on current page."

                # User provided a response — add to history, re-observe on next step
                print(f"  User responded: {user_response[:100]}")
                history.append({
                    "role": "user",
                    "content": f"You asked: {question}\nUser responded: {user_response}",
                })
                # Break batch — re-observe so LLM can act on user's response
                break

            # Stuck detection (track recent actions)
            action_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
            last_actions.append(action_key)
            if len(last_actions) > 6:
                last_actions.pop(0)

            stuck_hint = _detect_stuck(last_actions)
            if stuck_hint:
                print(f"  Stuck detected — {stuck_hint[:60]}")
                history.append({"role": "user", "content": stuck_hint})
                last_actions.clear()
                scroll_streak = 0

            # Execute action
            exec_result = await _execute_tool(bridge, tool_name, tool_args)
            print(f"  Result: {json.dumps(exec_result)[:100]}")

            _save_json(step_dir / f"action{'_' + str(ai_idx + 1) if len(actions) > 1 else ''}.json", {
                "action": tool_name, "params": tool_args,
                "reasoning": reasoning, "result": exec_result,
            })

            history.append({
                "role": "user",
                "content": f"You performed: {tool_name}({json.dumps(tool_args)}) → Result: {json.dumps(exec_result)[:200]}",
            })

            action_texts.append(_friendly_action(tool_name, tool_args))

            # If action failed, stop executing remaining actions
            if isinstance(exec_result, dict) and exec_result.get("error"):
                print(f"  Action failed, skipping remaining batch actions")
                break

            # Small delay between batched actions (no delay on last one — we'll re-observe anyway)
            if ai_idx < len(actions) - 1:
                await _sleep(0.15)

        if _cancelled():
            return _cancel_return(step)

        # Track scroll-only steps — if agent scrolls too many times without
        # clicking/typing/navigating, it's probably lost
        step_tool_names = [a["tool_name"] for a in actions]
        is_scroll_only = all(t in ("scroll", "wait") for t in step_tool_names)
        if is_scroll_only:
            scroll_streak += 1
        else:
            scroll_streak = 0

        if scroll_streak >= 4:
            print("  Scroll loop detected — injecting redirect hint")
            history.append({
                "role": "user",
                "content": (
                    "STOP SCROLLING. You have been scrolling for many steps without "
                    "taking any meaningful action. You are likely lost on this page. "
                    "Change your strategy NOW:\n"
                    "- If you can't find what you need, navigate() directly to a "
                    "relevant website (e.g. Google Flights, Booking.com, Amazon, etc.)\n"
                    "- Or go_back() and try a different search query\n"
                    "- Or click on a visible result instead of scrolling past it"
                ),
            })
            scroll_streak = 0

        # Notify step callback ONCE per step with a POST-action screenshot
        if on_step and action_texts:
            step_text = " → ".join(action_texts)
            # Take fresh screenshot showing the result of the actions
            post_screenshot = full_screenshot  # fallback to pre-action
            try:
                ss = await asyncio.wait_for(bridge.screenshot(), timeout=5.0)
                if isinstance(ss, dict) and ss.get("image"):
                    _, post_screenshot = process_screenshot(ss["image"])
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
        max_tokens: int = 512,
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


async def _observe(bridge: BrowserBridge) -> tuple[str, str, str]:
    """Extract DOM and take screenshot in parallel.

    Returns (dom_text, llm_screenshot_b64, full_screenshot_b64).
    """
    dom_coro = bridge.extract_dom()
    ss_coro = asyncio.wait_for(bridge.screenshot(), timeout=5.0)

    results = await asyncio.gather(dom_coro, ss_coro, return_exceptions=True)

    # Process DOM
    if isinstance(results[0], Exception):
        print(f"  DOM extraction failed: {results[0]}")
        dom_text = (
            "Page: New Tab (blank/internal page)\n"
            "URL: chrome://newtab\n"
            "Scroll: 0%\n\n"
            "This is a blank or internal Chrome page. Navigate to a website to start."
        )
    else:
        dom = DOMState.from_raw(results[0])
        dom_text = dom.format_for_llm()

    # Process screenshot — small for LLM, full for saving/Telegram
    llm_b64 = ""
    full_b64 = ""
    if isinstance(results[1], Exception):
        print(f"  Screenshot failed: {results[1]}")
    elif isinstance(results[1], dict) and "error" in results[1]:
        print(f"  Screenshot error: {results[1]['error']}")
    elif isinstance(results[1], dict):
        llm_b64, full_b64 = process_screenshot(results[1]["image"])

    return dom_text, llm_b64, full_b64


# Actions that target a DOM element by selector
_ELEMENT_ACTIONS = {
    "click", "type_text", "select_option", "hover",
    "double_click", "right_click", "extract_text",
}


async def _execute_tool(bridge, tool_name: str, args: dict) -> dict:
    """Execute a tool action on the browser."""
    try:
        selector = args.get("selector", "")

        # Smart-resolve selector for element-targeting actions
        if selector and tool_name in _ELEMENT_ACTIONS:
            resolved = await bridge.resolve_element(selector)
            if isinstance(resolved, dict) and resolved.get("error"):
                return resolved
            if isinstance(resolved, dict) and resolved.get("selector"):
                selector = resolved["selector"]

        if tool_name == "click":
            return await bridge.click(selector)

        elif tool_name == "type_text":
            return await bridge.type_text(selector, args["text"])

        elif tool_name == "select_option":
            return await bridge.select_option(selector, args["value"])

        elif tool_name == "scroll":
            return await bridge.scroll(args.get("direction", "down"))

        elif tool_name == "navigate":
            return await bridge.navigate(args["url"])

        elif tool_name == "go_back":
            return await bridge.go_back()

        elif tool_name == "wait":
            seconds = max(1, min(5, args.get("seconds", 1)))
            return await bridge.wait(seconds)

        elif tool_name == "extract_text":
            result = await bridge.evaluate_js(
                f'document.querySelector({json.dumps(selector)})?.innerText || ""'
            )
            return result

        elif tool_name == "hover":
            return await bridge.hover(selector)

        elif tool_name == "double_click":
            return await bridge.double_click(selector)

        elif tool_name == "right_click":
            return await bridge.right_click(selector)

        elif tool_name == "press_key":
            key = args.get("key", "Enter")
            sel = args.get("selector")
            # Resolve press_key selector too if present
            if sel:
                resolved = await bridge.resolve_element(sel)
                if isinstance(resolved, dict) and resolved.get("selector"):
                    sel = resolved["selector"]
                elif isinstance(resolved, dict) and resolved.get("error"):
                    return resolved
            return await bridge.press_key(key, sel)

        elif tool_name == "drag_drop":
            from_sel = args.get("from_sel", "")
            to_sel = args.get("to_sel", "")
            # Resolve both selectors
            for label, s in [("source", from_sel), ("target", to_sel)]:
                if s:
                    r = await bridge.resolve_element(s)
                    if isinstance(r, dict) and r.get("error"):
                        return {"error": f"{label} element not found"}
            r1 = await bridge.resolve_element(from_sel)
            r2 = await bridge.resolve_element(to_sel)
            return await bridge.drag_drop(
                r1.get("selector", from_sel),
                r2.get("selector", to_sel),
            )

        elif tool_name == "go_forward":
            return await bridge.go_forward()

        elif tool_name == "new_tab":
            return await bridge.new_tab(args.get("url"))

        elif tool_name == "switch_tab":
            return await bridge.switch_tab(args["index"])

        elif tool_name == "close_tab":
            return await bridge.close_tab()

        elif tool_name == "list_tabs":
            return await bridge.get_tabs()

        elif tool_name == "zoom":
            level = args.get("level", 100)
            return await bridge.zoom(level)

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": str(e)}


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep last MAX_HISTORY entries, summarize older ones."""
    if len(history) <= MAX_HISTORY:
        return list(history)

    older = history[:-MAX_HISTORY]
    recent = history[-MAX_HISTORY:]

    summary_parts = []
    for msg in older:
        content = msg.get("content", "")
        if isinstance(content, str) and "You performed:" in content:
            summary_parts.append(content.split("→")[0].strip())

    summary = "Previous actions: " + "; ".join(summary_parts[-5:])
    return [{"role": "user", "content": summary}] + recent



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


def _selector_label(selector: str) -> str:
    """Turn a CSS selector into a short, human-readable label."""
    if not selector:
        return "element"
    s = selector.strip()
    # "#search-btn" → "search-btn"
    if s.startswith("#"):
        return s[1:][:30]
    # "input[name='q']" → "input 'q'"
    import re as _re
    attr_m = _re.search(r"\[(?:name|placeholder|aria-label|title|alt)=['\"]([^'\"]{1,30})", s)
    if attr_m:
        return f'"{attr_m.group(1)}"'
    # "a[href='/login']" → "link /login"
    href_m = _re.search(r"\[href=['\"]([^'\"]{1,40})", s)
    if href_m:
        return href_m.group(1)[:30]
    # Keep it short
    if len(s) > 35:
        return s[:32] + "..."
    return s


def _friendly_action(tool_name: str, args: dict) -> str:
    """Format a tool action as a nice user-friendly description with emojis."""
    sel = args.get("selector", "")
    label = _selector_label(sel)
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
        if sel:
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
        return f"🔄 Dragging {_selector_label(args.get('from_sel', ''))} to {_selector_label(args.get('to_sel', ''))}"
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
