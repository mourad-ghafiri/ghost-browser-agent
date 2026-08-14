"""LLM client — JSON action dispatch with plan/progress state."""

import json
import re

from openai import AsyncOpenAI


class ParseError(Exception):
    """Model response contained no parseable action JSON."""

    def __init__(self, raw: str):
        super().__init__(f"unparseable model response: {raw[:200]}")
        self.raw = raw


SYSTEM_PROMPT = """\
You are a browser agent with VISION. Each step you receive: a text outline of the visible page where every interactive element is numbered [N], and a screenshot where the same numbers appear as small red badges. Act on elements by their index number, like a human pointing at what they see.

The page may be in ANY language. Read it natively — the outline shows the page's own words verbatim. Answer in the language of the GOAL.

## Actions

click(index)                        Click element [N]
double_click(index) / right_click(index) / hover(index)
type_text(index, text)              Clear field [N] and type text
press_key(key, index?)              Enter, Escape, Tab, Backspace, Delete, Space, ArrowUp/Down/Left/Right, Home, End, PageUp/PageDown
select_option(index, value)         Pick option by value or its exact visible label
drag_drop(from_index, to_index)     Drag [A] onto [B]
extract_text(index)                 Read element [N]'s full text
scroll(direction, index?)           "up"/"down"; with index scrolls that container
navigate(url)                       Go to URL (full https://...)
go_back() / go_forward()            Browser history
new_tab(url), switch_tab(index), close_tab(), list_tabs()   Tabs (0-based; never close the last tab)
wait(seconds)                       Wait 1-5 seconds
zoom(level)                         Page zoom % (100 = normal)
ask_user(question)                  Ask the user for confirmation or information
done(result)                        Goal achieved — return the answer

Indices are valid ONLY for the current step's outline/screenshot. If an action reports the element is gone or the observation is stale, you'll get a fresh numbered view next step — pick again from the new numbers.

## Response format — reply with ONLY this JSON object

{"plan": "1. open site [done] 2. search [now] 3. read result 4. answer",
 "progress": "search page open, query not yet typed",
 "actions": [{"action": "type_text", "params": {"index": 2, "text": "query"}},
             {"action": "press_key", "params": {"key": "Enter"}}]}

- Always include "plan": your numbered steps toward the GOAL, marking finished ones [done] and the current one [now]. Keep it under 160 chars and update it every step.
- Always include "progress": one sentence on where you are (under 120 chars).
- Batch several actions ONLY when the outcome is predictable (type + Enter, fill several form fields). Use a single action whenever you must see the result first: {"plan": ..., "progress": ..., "action": "click", "params": {"index": 7}}

## Self-check — every step before acting

Compare PROGRESS against GOAL. Goal fully achieved → reply with done(result) carrying the answer. Never call done() because you are stuck — change strategy instead (different element, different site, go_back).

## ask_user() — REQUIRED before sensitive actions

Passwords or credentials · payments, purchases, subscriptions · personal data (SSN, card, address, phone) · deleting accounts or data · security settings (2FA, email, password) · any irreversible action. Also use it when you need information the task doesn't provide. If the user rejects, the task stops.

## Overlays FIRST

If any modal, banner, or popup is visible, handle it before anything else — even mid-task:
- Cookie/consent dialogs: click the option that ACCEPTS, in whatever language the page uses. Never the reject or settings/preferences option.
- Other overlays (newsletter, sign-in prompt, notifications, age gate): click their close/dismiss control or press Escape.

## CAPTCHA — you CAN see the images (page auto-zooms for you)

1. Read the instruction. 2. Study every tile in the screenshot. 3. Click ONE matching tile per step — never batch CAPTCHA clicks — so you see the updated grid each time. 4. When all matches are selected, click verify. Solve it yourself; never ask the user.

## Strategy

- Outline and screenshot show ONLY the current viewport; scroll to reveal more. After 2-3 fruitless scrolls, change approach: navigate() to a better site, new query, or go_back().
- Task mentions a URL → navigate() there directly.
- Booking/shopping: go straight to the relevant site (Google Flights, Booking.com, Amazon) — search-result widgets are rarely interactive.
- "best"/"cheapest" → compare 3-5 options (ratings, reviews, price) before deciding, and ask_user() with your recommendation before any purchase.
- Truncated text ("…") → extract_text(index) for the full content.
- Compare things across tabs; list_tabs() before switching.
"""


class AIClient:
    """LLM client — sends goal-centric context, parses JSON action response."""

    def __init__(
        self,
        model: str = "qwen3.5-27b",
        api_base: str = "http://localhost:1234/v1",
        vision_enabled: bool = True,
        temperature: float = 0.3,
        max_tokens: int = 768,
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
            "Look at this page screenshot and page outline. "
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
                # Thinking models (Qwen3.8) spend reasoning tokens from this
                # budget before emitting the answer — a tiny cap returns empty
                max_tokens=512,
            )
            raw = (response.choices[0].message.content or "").strip().lower()
            raw = _strip_think(raw)
            return "yes" in raw
        except Exception:
            return False

    async def step(self, context: str, screenshot_b64: str) -> dict:
        """Send the observation context to the LLM, parse its response.

        Returns {"plan": str|None, "progress": str|None,
                 "actions": [{"tool_name", "tool_args", "reasoning"}, ...]}.
        Raises ParseError if the model can't produce valid action JSON
        even after one nudge retry.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        if screenshot_b64 and self._vision_supported:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": context},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                ],
            })
        else:
            messages.append({"role": "user", "content": context})

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        except Exception as e:
            # If we sent an image, retry without it — but only disable vision
            # permanently if the error is clearly vision-related
            if screenshot_b64 and self._vision_supported:
                err_str = str(e).lower()
                is_vision_error = any(k in err_str for k in ("image", "vision", "multimodal", "content type"))
                if is_vision_error:
                    self._vision_supported = False
                    print("  Vision not supported by model, disabling for this session")
                else:
                    print("  LLM error with image, retrying text-only (vision stays enabled)")
                messages[-1] = {"role": "user", "content": context}
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
            else:
                raise

        msg = response.choices[0].message
        raw = msg.content or ""
        # Thinking models served by LM Studio return their chain of thought
        # in a separate reasoning_content field
        thinking = getattr(msg, "reasoning_content", None) or ""

        try:
            parsed = parse_response(raw)
        except ParseError:
            # One nudge retry — force JSON output
            print("  No JSON in response, nudging model to output action...")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Output ONLY the JSON action object now. No other text."})
            retry = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._temperature,
                # Full budget: thinking models burn reasoning tokens first,
                # so a small cap here would truncate before any JSON appears
                max_tokens=self._max_tokens,
            )
            retry_msg = retry.choices[0].message
            thinking = getattr(retry_msg, "reasoning_content", None) or thinking
            parsed = parse_response(retry_msg.content or "")  # raises ParseError if still bad

        if thinking:
            for action in parsed["actions"]:
                if not action.get("reasoning"):
                    action["reasoning"] = thinking
        return parsed


def _strip_think(raw: str) -> str:
    """Remove <think> blocks (inline-thinking models) and fake turn markers."""
    raw = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<think(?:ing)?>.*", "", raw, flags=re.DOTALL)  # unclosed <think>
    for stop in ("<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|end|>"):
        idx = raw.find(stop)
        if idx != -1:
            raw = raw[:idx]
    return raw


def parse_response(raw: str) -> dict:
    """Parse the model's JSON response.

    Returns {"plan": str|None, "progress": str|None, "actions": [...]}.
    Raises ParseError when no recognizable action JSON is found.
    """
    text = _strip_think(raw).strip()

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

    if not isinstance(obj, dict):
        raise ParseError(raw)

    plan = obj.get("plan")
    progress = obj.get("progress")
    plan = str(plan)[:300] if plan else None
    progress = str(progress)[:300] if progress else None
    reasoning = obj.get("thinking", obj.get("reasoning", ""))

    actions: list[dict] = []
    if isinstance(obj.get("actions"), list):
        for a in obj["actions"]:
            if isinstance(a, dict) and "action" in a:
                actions.append(_normalize(a, reasoning))
    if not actions and "action" in obj:
        actions.append(_normalize(obj, reasoning))

    if not actions:
        raise ParseError(raw)

    return {"plan": plan, "progress": progress, "actions": actions}


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


def _normalize(obj: dict, fallback_reasoning: str = "") -> dict:
    """Normalize a single parsed action to the standard shape."""
    params = obj.get("params", {})
    if not isinstance(params, dict):
        params = {}
    return {
        "tool_name": obj.get("action", "done"),
        "tool_args": params,
        "reasoning": obj.get("thinking", obj.get("reasoning", "")) or fallback_reasoning,
    }
