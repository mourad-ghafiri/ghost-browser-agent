"""LLM client — JSON-based tool dispatch with multi-action support."""

import json
import re

from openai import AsyncOpenAI


SYSTEM_PROMPT = """\
You are an expert browser automation agent with VISION. You receive BOTH a screenshot of the page AND clean HTML. You can SEE everything in the screenshot — images, icons, layouts, CAPTCHAs, charts, colors, text rendered in images. Use your vision alongside the HTML to understand the page fully. You interact with elements using CSS selectors that you write yourself based on the HTML you see.

## Actions

click(selector)                  Click element
double_click(selector)           Double-click element
right_click(selector)            Right-click element
type_text(selector, text)        Clear field and type text
press_key(key)                   Press key: Enter, Escape, Tab, Backspace, Delete, Space, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Home, End, PageUp, PageDown
press_key(key, selector)         Press key on specific element
select_option(selector, value)   Choose dropdown option
hover(selector)                  Hover over element
drag_drop(from_sel, to_sel)      Drag and drop
navigate(url)                    Go to URL (full https://...)
go_back()                        Browser back
go_forward()                     Browser forward
scroll(direction)                Scroll "up" or "down"
new_tab(url)                     Open new tab
switch_tab(index)                Switch tab (0-based)
close_tab()                      Close current tab (fails if it's the last tab)
list_tabs()                      Get list of all open tabs (index, title, url, active)
wait(seconds)                    Wait 1-5 seconds
zoom(level)                      Set page zoom: 100 = normal, 200 = 2x, 150 = 1.5x
extract_text(selector)           Read element text
ask_user(question)               Ask the user for confirmation or information
done(result)                     Task complete — return answer

## CSS selectors

Write CSS selectors based on the HTML you see. Use the most reliable selector:
- By id: "#search-input"
- By class: "button.submit-btn"
- By attribute: "input[name='q']", "a[href='/login']", "[data-testid='search']"
- By tag + text context: use nearby attributes to disambiguate
- Combine for precision: "form input[type='text']", "#main a.nav-link"

Keep selectors simple and specific. Prefer id > data-testid > name > class > tag structure.

## Response format

Reply with ONLY a JSON object.

Single action:
{"action": "click", "params": {"selector": "#search-btn"}}

Multiple actions (executed in sequence without re-observing the page between them):
{"actions": [{"action": "type_text", "params": {"selector": "input[name='q']", "text": "query"}}, {"action": "press_key", "params": {"key": "Enter"}}]}

Batch multiple actions when the outcome is predictable:
- Type into a field and press Enter
- Fill multiple form fields
- Type and submit

Use single action when you need to see the result first (clicks that change the page, navigation, scrolling to find content).

## Critical: ask_user() for sensitive actions

You MUST call ask_user() BEFORE performing any of these:
- Entering passwords or credentials
- Submitting payments or financial transactions
- Filling personal/private information (SSN, credit card, address, phone)
- Deleting accounts or data
- Changing security settings (2FA, email, password)
- Confirming purchases or subscriptions
- Any irreversible action

Also use ask_user() when you need information not provided in the task (login credentials, preferences, choices between options).

The user will reply with their answer or reject the action. If rejected, the task stops but the browser stays on the current page.

## How to think

Before acting, ALWAYS reason about your approach:
1. What is the user's REAL goal? Understand the full intent, not just the literal words.
2. What is my plan to achieve it? Break complex tasks into clear steps.
3. Where am I in the plan? Track progress and adjust as needed.
4. What is the BEST next action? Not just any action — the smartest one.

For research/comparison tasks ("find the best", "cheapest", "compare"):
- GATHER information first — scroll through results, read ratings, reviews, and prices.
- COMPARE multiple options before choosing. Don't pick the first thing you see.
- "Best" means highest rated AND good value. "Cheapest" means lowest price WITH good reviews (not junk).
- "Best and cheapest" means best value — high ratings at a reasonable price. Read at least 3-5 options.
- Use extract_text() to read full descriptions and reviews when the HTML is truncated.
- Present your findings to the user with ask_user() before making a purchase decision.

For shopping tasks:
- Search with specific, refined queries. Add keywords like "best rated", "bestseller" if relevant.
- Look at star ratings, number of reviews, and price together — not just one factor.
- Sort/filter smartly. Use the site's built-in filters (rating, price range, bestseller).
- Open promising product pages to check details before deciding.
- If the user says "buy", always ask_user() with your recommendation and price before adding to cart.

## Strategy

- The HTML and screenshot show ONLY what's currently visible in the viewport. Content above or below is not shown.
- If the element or information you need is not in the HTML, scroll("down") or scroll("up") to reveal more content. After scrolling you'll see the new viewport.
- Check the scroll percentage — if it's low, there's more content below. Scroll to find it.
- When the task mentions a URL, use navigate() directly — don't click links.
- ALWAYS dismiss overlays, popups, cookie banners, consent dialogs, and newsletter modals FIRST before doing anything else. Accept cookies/terms when needed to proceed.
- Close any "sign in" prompts, notification requests, or chat widgets that block the page.
- CAPTCHA solving: You have VISION — you CAN see images in the screenshot. The page is automatically zoomed in for you when a CAPTCHA is detected, so images are large and clear.
  1. READ the instruction (e.g. "select all images with boats")
  2. LOOK at the screenshot carefully — analyze EACH tile in the grid individually
  3. CLICK only ONE matching tile per step (single action, NOT batched) — so you can re-observe after each click
  4. After selecting ALL matching tiles, click verify/submit
  5. If a new CAPTCHA appears, repeat from step 1
  Do NOT ask the user for help with CAPTCHAs. You can see the images. Solve them yourself.
  IMPORTANT: For CAPTCHAs, NEVER batch multiple clicks. Use ONE click per step so you see the updated page after each selection.
- For search: batch type_text + press_key("Enter") together.
- For forms: batch all field fills, then submit.
- Use extract_text(selector) to read the full text of an element when the HTML text is truncated.
- Call done() as soon as you have the answer.
- NEVER scroll more than 3 times in a row without clicking something. If you've scrolled 2-3 times and still can't find what you need, the content is not on this page — navigate() to a better site or go_back() and try a different query.
- For booking/shopping, go DIRECTLY to the relevant site: Google Flights, Booking.com, Amazon, etc. Don't try to use Google search result widgets — they are often not interactive.
- If stuck, try a COMPLETELY different approach — navigate to a different URL, use a different search query, or go_back().
- Use tabs for comparing things: open products/pages in new tabs, switch between them, then close when done.
- Use list_tabs() to see what's open before switching. Tab indices are 0-based.
- Don't close the last tab — open a new one first if you need to navigate away.

## Examples

{"action": "navigate", "params": {"url": "https://www.google.com"}}
{"action": "click", "params": {"selector": "#search-btn"}}
{"actions": [{"action": "type_text", "params": {"selector": "textarea[name='q']", "text": "weather in Paris"}}, {"action": "press_key", "params": {"key": "Enter"}}]}
{"action": "scroll", "params": {"direction": "down"}}
{"action": "click", "params": {"selector": "a[href='/flights']"}}
{"action": "type_text", "params": {"selector": "input[placeholder='Where to?']", "text": "Bali"}}
{"action": "ask_user", "params": {"question": "I found a flight for $450. Should I proceed with booking? I'll need your full name and payment details."}}
{"action": "done", "params": {"result": "The temperature is 12°C"}}
"""


class AIClient:
    """LLM client — sends page state, parses JSON action response."""

    def __init__(
        self,
        model: str = "qwen3.5-27b",
        api_base: str = "http://localhost:1234/v1",
        vision_enabled: bool = True,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ):
        self.model = model
        self.client = AsyncOpenAI(base_url=api_base, api_key="not-needed")
        self._vision_enabled = vision_enabled
        self._vision_supported = vision_enabled
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def detect_captcha(
        self,
        dom_text: str,
        screenshot_b64: str,
    ) -> bool:
        """Ask LLM to check if the current page has a CAPTCHA challenge."""
        prompt = (
            "Look at this page screenshot and HTML. "
            "Is there a CAPTCHA challenge visible (reCAPTCHA, hCaptcha, image grid, "
            "checkbox challenge, puzzle, etc.)?\n"
            "Reply with ONLY: yes or no\n\n"
            f"{dom_text[:3000]}"
        )

        if screenshot_b64 and self._vision_supported:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
            ]
        else:
            content = prompt

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0.0,
                max_tokens=10,
            )
            raw = (response.choices[0].message.content or "").strip().lower()
            # Strip think blocks
            raw = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw, flags=re.DOTALL)
            raw = re.sub(r"<think(?:ing)?>.*", "", raw, flags=re.DOTALL)
            return "yes" in raw
        except Exception:
            return False

    async def step(
        self,
        goal: str,
        dom_text: str,
        screenshot_b64: str,
        history: list[dict],
    ) -> list[dict]:
        """Send state to LLM, get back parsed action(s).

        Returns list of dicts, each with keys: tool_name, tool_args, reasoning.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)

        # Current observation — try with image first, fall back to text-only
        user_text = f"Task: {goal}\n\n{dom_text}"

        if screenshot_b64 and self._vision_supported:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                ],
            })
        else:
            messages.append({"role": "user", "content": user_text})

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except Exception as e:
            # If we sent an image, retry without it — but only disable vision
            # permanently if the error is clearly vision-related (400 bad request)
            if screenshot_b64 and self._vision_supported:
                err_str = str(e).lower()
                is_vision_error = any(k in err_str for k in ("image", "vision", "multimodal", "content type"))
                if is_vision_error:
                    self._vision_supported = False
                    print("  Vision not supported by model, disabling for this session")
                else:
                    print(f"  LLM error with image, retrying text-only (vision stays enabled)")
                messages[-1] = {"role": "user", "content": user_text}
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
            else:
                raise

        raw = response.choices[0].message.content or ""
        actions = _parse_actions(raw)

        # If parsing failed (model spent all tokens on reasoning, no JSON produced),
        # retry once with a short nudge to force JSON output
        if (len(actions) == 1
                and actions[0]["tool_args"].get("result", "").startswith("Model returned unparseable")):
            print("  No JSON in response, nudging model to output action...")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Output ONLY the JSON action now. No text."})
            retry = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=256,
            )
            raw2 = retry.choices[0].message.content or ""
            retry_actions = _parse_actions(raw2)
            if not (len(retry_actions) == 1
                    and retry_actions[0]["tool_args"].get("result", "").startswith("Model returned unparseable")):
                return retry_actions

        return actions


def _parse_actions(raw: str) -> list[dict]:
    """Parse JSON action(s) from model response."""
    # Strip think/thinking blocks (Qwen3 thinking mode) — including unclosed ones
    raw = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<think(?:ing)?>.*", "", raw, flags=re.DOTALL)  # unclosed <think>

    # Truncate at special tokens (Qwen generates fake turns after its response)
    for stop in ("<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|end|>"):
        idx = raw.find(stop)
        if idx != -1:
            raw = raw[:idx]

    text = raw.strip()

    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # First try: the whole (cleaned) response is JSON
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        pass

    # Second try: extract the first complete JSON object using balanced braces
    if obj is None:
        obj = _extract_first_json(text)

    if obj is None:
        # Give up — return done with the raw text
        return [{
            "tool_name": "done",
            "tool_args": {"result": f"Model returned unparseable response: {raw[:200]}"},
            "reasoning": raw,
        }]

    # Multi-action format: {"actions": [...]}
    if "actions" in obj and isinstance(obj["actions"], list):
        reasoning = obj.get("thinking", obj.get("reasoning", ""))
        actions = []
        for a in obj["actions"]:
            if isinstance(a, dict) and "action" in a:
                n = _normalize(a)
                if not n.get("reasoning") and reasoning:
                    n["reasoning"] = reasoning
                actions.append(n)
        return actions if actions else [_normalize(obj)]

    # Single action format: {"action": "...", "params": {...}}
    return [_normalize(obj)]


def _extract_first_json(text: str) -> dict | None:
    """Extract the first balanced JSON object from text."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _normalize(obj: dict) -> dict:
    """Normalize parsed JSON to standard format."""
    action = obj.get("action", "done")
    params = obj.get("params", {})

    # Handle thinking/reasoning if model included it
    reasoning = obj.get("thinking", obj.get("reasoning", ""))

    return {
        "tool_name": action,
        "tool_args": params,
        "reasoning": reasoning,
    }
