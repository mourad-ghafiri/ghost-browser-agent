// Ghost Extension Bridge — WebSocket client + command router

const WS_URL = "ws://127.0.0.1:7331";
const HEARTBEAT_MS = 20_000;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

let ws = null;
let reconnectDelay = RECONNECT_BASE_MS;
let heartbeatTimer = null;

// --- WebSocket lifecycle ---

function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;

  try {
    ws = new WebSocket(WS_URL);
  } catch {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log("[ghost] connected");
    reconnectDelay = RECONNECT_BASE_MS;
    startHeartbeat();
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "pong") return;
    console.log("[ghost] recv:", msg.command, msg.id);
    try {
      const result = await handleCommand(msg);
      console.log("[ghost] done:", msg.command, msg.id);
      send({ id: msg.id, result });
    } catch (err) {
      console.error("[ghost] error:", msg.command, err);
      send({ id: msg.id, result: { error: err.message || "Unknown extension error" } });
    }
  };

  ws.onclose = () => {
    console.log("[ghost] disconnected");
    stopHeartbeat();
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = () => {
    ws?.close();
  };
}

function send(data) {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => send({ type: "ping" }), HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function scheduleReconnect() {
  const delay = Math.min(reconnectDelay + Math.random() * 500, RECONNECT_MAX_MS);
  setTimeout(connect, delay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

// Keepalive alarm — restarts connection if WS drops while service worker is alive
chrome.alarms.create("ghost-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "ghost-keepalive") connect();
});

// --- Active tab helper ---

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// --- Command router ---

async function handleCommand(msg) {
  const { command, params = {} } = msg;
  try {
    switch (command) {
      case "navigate":
        return await cmdNavigate(params);
      case "extract_dom":
        return await cmdExtractDom();
      case "click":
        return await cmdClick(params);
      case "type":
        return await cmdType(params);
      case "select":
        return await cmdSelect(params);
      case "scroll":
        return await cmdScroll(params);
      case "hover":
        return await cmdHover(params);
      case "screenshot":
        return await cmdScreenshot();
      case "wait":
        return await cmdWait(params);
      case "evaluate_js":
        return await cmdEvaluateJs(params);
      case "back":
        return await cmdBack();
      case "forward":
        return await cmdForward();
      case "double_click":
        return await cmdDoubleClick(params);
      case "right_click":
        return await cmdRightClick(params);
      case "press_key":
        return await cmdPressKey(params);
      case "drag_drop":
        return await cmdDragDrop(params);
      case "new_tab":
        return await cmdNewTab(params);
      case "switch_tab":
        return await cmdSwitchTab(params);
      case "close_tab":
        return await cmdCloseTab();
      case "get_tabs":
        return await cmdGetTabs();
      case "get_url":
        return await cmdGetUrl();
      case "resolve":
        return await cmdResolve(params);
      case "zoom":
        return await cmdZoom(params);
      default:
        return { error: `unknown command: ${command}` };
    }
  } catch (err) {
    return { error: err.message || "Unknown command error" };
  }
}

// --- Commands ---

async function cmdNavigate({ url }) {
  const tab = await getActiveTab();
  await chrome.tabs.update(tab.id, { url });
  // Wait for navigation to complete
  await new Promise((resolve) => {
    const listener = (tabId, info) => {
      if (tabId === tab.id && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, 15_000);
  });
  // Small delay for SPA hydration / dynamic content
  await sleep(500);
  injectOverlay(tab.id);
  return { ok: true };
}

async function cmdExtractDom() {
  const tab = await getActiveTab();
  if (!tab || tab.url?.startsWith("chrome://")) {
    return { html: "", selectorMap: {}, url: tab?.url || "", title: tab?.title || "", scroll: { top: 0, height: 0, viewport: 0 } };
  }
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: extractDomFromPage,
  });
  return result.result;
}

async function cmdClick({ selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      // Scroll into view
      el.scrollIntoView({ block: "center", behavior: "instant" });

      // Get element center for realistic mouse events
      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const eventOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };

      // Dispatch full mouse event sequence (critical for SPAs like React/Vue/Angular)
      el.dispatchEvent(new PointerEvent("pointerover", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseover", eventOpts));
      el.dispatchEvent(new PointerEvent("pointerenter", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseenter", eventOpts));
      el.dispatchEvent(new PointerEvent("pointerdown", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mousedown", eventOpts));
      el.focus();
      el.dispatchEvent(new PointerEvent("pointerup", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseup", eventOpts));
      el.dispatchEvent(new MouseEvent("click", eventOpts));

      // For links, if the SPA prevented default, the native click handles it
      // For non-links, the event sequence above covers React onClick etc.
      return { ok: true, tag: el.tagName.toLowerCase() };
    },
    args: [selector],
  });
  // Wait for SPA route change / dynamic content after click
  await waitForPageSettle(tab.id);
  return result.result;
}

async function cmdType({ selector, text, clear }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel, txt, clr) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      el.scrollIntoView({ block: "center", behavior: "instant" });

      // Focus with proper event sequence
      el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      el.focus();
      el.dispatchEvent(new FocusEvent("focus", { bubbles: false }));

      // Clear existing value
      if (clr !== false) {
        // Use native setter to bypass React's synthetic event system
        const nativeSetter = Object.getOwnPropertyDescriptor(
          Object.getPrototypeOf(el), "value"
        )?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        if (nativeSetter) {
          nativeSetter.call(el, "");
        } else {
          el.value = "";
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }

      // Type character by character with proper keyboard events
      for (const ch of txt) {
        el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, code: `Key${ch.toUpperCase()}`, bubbles: true }));
        el.dispatchEvent(new KeyboardEvent("keypress", { key: ch, code: `Key${ch.toUpperCase()}`, bubbles: true }));

        // Use native setter for React compatibility
        const nativeSetter = Object.getOwnPropertyDescriptor(
          Object.getPrototypeOf(el), "value"
        )?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        if (nativeSetter) {
          nativeSetter.call(el, el.value + ch);
        } else {
          el.value += ch;
        }

        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
        el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, code: `Key${ch.toUpperCase()}`, bubbles: true }));
      }

      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true };
    },
    args: [selector, text, clear],
  });
  return result.result;
}

async function cmdSelect({ selector, value }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel, val) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      el.scrollIntoView({ block: "center", behavior: "instant" });
      el.focus();

      // Use native setter for React compatibility
      const nativeSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      if (nativeSetter) {
        nativeSetter.call(el, val);
      } else {
        el.value = val;
      }

      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true };
    },
    args: [selector, value],
  });
  return result.result;
}

async function cmdScroll({ direction, selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (dir, sel) => {
      let target = null;
      if (sel) {
        target = document.querySelector(sel);
      }

      const amount = window.innerHeight * 0.7;
      const delta = dir === "up" ? -amount : amount;

      if (target) {
        // Scroll specific element into view
        target.scrollIntoView({ block: "center", behavior: "smooth" });
      } else {
        // Scroll the page — try documentElement, then body, then find scrollable container
        const scrollable = document.scrollingElement || document.documentElement;
        scrollable.scrollBy({ top: delta, behavior: "smooth" });
      }

      // Return new scroll position
      const scrollTop = window.scrollY || document.documentElement.scrollTop;
      const scrollHeight = document.documentElement.scrollHeight;
      const viewport = window.innerHeight;
      return {
        ok: true,
        scrollTop: Math.round(scrollTop),
        scrollHeight: Math.round(scrollHeight),
        atTop: scrollTop <= 5,
        atBottom: scrollTop + viewport >= scrollHeight - 5,
      };
    },
    args: [direction, selector || null],
  });
  // Let smooth scroll complete
  await sleep(400);
  return result.result;
}

async function cmdHover({ selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      el.scrollIntoView({ block: "center", behavior: "instant" });

      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const eventOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y };

      // Full hover event sequence
      el.dispatchEvent(new PointerEvent("pointerover", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseover", eventOpts));
      el.dispatchEvent(new PointerEvent("pointerenter", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseenter", eventOpts));
      el.dispatchEvent(new PointerEvent("pointermove", { ...eventOpts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mousemove", eventOpts));

      return { ok: true };
    },
    args: [selector],
  });
  // Wait for hover-triggered content (tooltips, dropdowns)
  await sleep(500);
  return result.result;
}

async function cmdScreenshot() {
  const tab = await getActiveTab();
  if (!tab) return { error: "no active tab" };

  const url = tab.url || "";
  // Can't capture chrome://, chrome-extension://, about:, or empty pages
  if (!url || url === "about:blank" || url.startsWith("chrome://") || url.startsWith("chrome-extension://") || url.startsWith("about:")) {
    return { error: "cannot capture internal/blank pages" };
  }

  // Wait for rendering
  await sleep(300);

  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "jpeg",
      quality: 60,
    });
    const base64 = dataUrl.replace(/^data:image\/\w+;base64,/, "");
    return { image: base64 };
  } catch (err) {
    // Gracefully handle permission errors (e.g. activeTab not in effect on some pages)
    return { error: err.message };
  }
}

async function cmdWait({ seconds }) {
  const ms = Math.min((seconds || 1) * 1000, 10_000);
  await sleep(ms);
  return { ok: true };
}

async function cmdEvaluateJs({ code }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (c) => {
      try {
        return { value: eval(c) };
      } catch (err) {
        return { error: err.message };
      }
    },
    args: [code],
  });
  return result.result;
}

async function cmdBack() {
  const tab = await getActiveTab();
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: () => history.back(),
  });
  // Wait for navigation
  await sleep(1500);
  return { ok: true };
}

async function cmdForward() {
  const tab = await getActiveTab();
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: () => history.forward(),
  });
  await sleep(1500);
  return { ok: true };
}

async function cmdDoubleClick({ selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      el.scrollIntoView({ block: "center", behavior: "instant" });

      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };

      // First click
      el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      el.dispatchEvent(new MouseEvent("click", opts));

      // Second click (detail: 2 marks it as double-click)
      el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mousedown", { ...opts, detail: 2 }));
      el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseup", { ...opts, detail: 2 }));
      el.dispatchEvent(new MouseEvent("click", { ...opts, detail: 2 }));
      el.dispatchEvent(new MouseEvent("dblclick", { ...opts, detail: 2 }));

      return { ok: true };
    },
    args: [selector],
  });
  await waitForPageSettle(tab.id);
  return result.result;
}

async function cmdRightClick({ selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel) => {
      const el = document.querySelector(sel);
      if (!el) return { error: "element not found" };

      el.scrollIntoView({ block: "center", behavior: "instant" });

      const rect = el.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 2 };

      el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerType: "mouse" }));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      el.dispatchEvent(new MouseEvent("contextmenu", { ...opts, button: 2 }));

      return { ok: true };
    },
    args: [selector],
  });
  await sleep(300);
  return result.result;
}

async function cmdPressKey({ key, selector }) {
  const tab = await getActiveTab();
  let scriptResult;
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "ISOLATED",
      func: (k, sel) => {
        // Key name mapping
        const KEY_MAP = {
          enter: { key: "Enter", code: "Enter", keyCode: 13 },
          escape: { key: "Escape", code: "Escape", keyCode: 27 },
          esc: { key: "Escape", code: "Escape", keyCode: 27 },
          tab: { key: "Tab", code: "Tab", keyCode: 9 },
          backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
          delete: { key: "Delete", code: "Delete", keyCode: 46 },
          space: { key: " ", code: "Space", keyCode: 32 },
          arrowup: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
          arrowdown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
          arrowleft: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 },
          arrowright: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 },
          home: { key: "Home", code: "Home", keyCode: 36 },
          end: { key: "End", code: "End", keyCode: 35 },
          pageup: { key: "PageUp", code: "PageUp", keyCode: 33 },
          pagedown: { key: "PageDown", code: "PageDown", keyCode: 34 },
        };

        const target = sel ? document.querySelector(sel) : document.activeElement || document.body;
        if (sel && !target) return { error: "element not found" };

        if (sel) {
          target.scrollIntoView({ block: "center", behavior: "instant" });
          target.focus();
        }

        const mapped = KEY_MAP[k.toLowerCase()] || { key: k, code: `Key${k.toUpperCase()}`, keyCode: k.charCodeAt(0) };

        const eventOpts = {
          key: mapped.key,
          code: mapped.code,
          keyCode: mapped.keyCode,
          which: mapped.keyCode,
          bubbles: true,
          cancelable: true,
        };

        target.dispatchEvent(new KeyboardEvent("keydown", eventOpts));
        target.dispatchEvent(new KeyboardEvent("keypress", eventOpts));
        target.dispatchEvent(new KeyboardEvent("keyup", eventOpts));

        // For Enter on forms, try to submit
        if (mapped.key === "Enter" && target.form) {
          target.form.requestSubmit?.() || target.form.submit();
        }

        return { ok: true, target: target.tagName?.toLowerCase() };
      },
      args: [key, selector || null],
    });
    scriptResult = result.result;
  } catch {
    // Page likely navigated away due to form submit — that's success
    scriptResult = { ok: true };
  }
  await waitForPageSettle(tab.id);
  return scriptResult;
}

async function cmdDragDrop({ fromSelector, toSelector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (fromSel, toSel) => {
      const from = document.querySelector(fromSel);
      const to = document.querySelector(toSel);
      if (!from) return { error: "source element not found" };
      if (!to) return { error: "target element not found" };

      from.scrollIntoView({ block: "center", behavior: "instant" });

      const fromRect = from.getBoundingClientRect();
      const toRect = to.getBoundingClientRect();
      const fromX = fromRect.left + fromRect.width / 2;
      const fromY = fromRect.top + fromRect.height / 2;
      const toX = toRect.left + toRect.width / 2;
      const toY = toRect.top + toRect.height / 2;

      // Create a DataTransfer object
      const dataTransfer = new DataTransfer();

      // --- Mouse-based drag sequence ---
      // 1. Pointer/mouse down on source
      from.dispatchEvent(new PointerEvent("pointerdown", {
        bubbles: true, cancelable: true, clientX: fromX, clientY: fromY, pointerType: "mouse",
      }));
      from.dispatchEvent(new MouseEvent("mousedown", {
        bubbles: true, cancelable: true, clientX: fromX, clientY: fromY,
      }));

      // 2. Drag start on source
      from.dispatchEvent(new DragEvent("dragstart", {
        bubbles: true, cancelable: true, clientX: fromX, clientY: fromY, dataTransfer,
      }));
      from.dispatchEvent(new DragEvent("drag", {
        bubbles: true, cancelable: true, clientX: fromX, clientY: fromY, dataTransfer,
      }));

      // 3. Move to target — simulate intermediate mouse moves
      const steps = 5;
      for (let i = 1; i <= steps; i++) {
        const ratio = i / steps;
        const cx = fromX + (toX - fromX) * ratio;
        const cy = fromY + (toY - fromY) * ratio;
        document.elementFromPoint(cx, cy)?.dispatchEvent(new DragEvent("dragover", {
          bubbles: true, cancelable: true, clientX: cx, clientY: cy, dataTransfer,
        }));
      }

      // 4. Enter + over target
      to.dispatchEvent(new DragEvent("dragenter", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY, dataTransfer,
      }));
      to.dispatchEvent(new DragEvent("dragover", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY, dataTransfer,
      }));

      // 5. Drop on target
      to.dispatchEvent(new DragEvent("drop", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY, dataTransfer,
      }));

      // 6. Drag end on source
      from.dispatchEvent(new DragEvent("dragend", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY, dataTransfer,
      }));

      // 7. Release pointer/mouse
      to.dispatchEvent(new PointerEvent("pointerup", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY, pointerType: "mouse",
      }));
      to.dispatchEvent(new MouseEvent("mouseup", {
        bubbles: true, cancelable: true, clientX: toX, clientY: toY,
      }));

      return { ok: true };
    },
    args: [fromSelector, toSelector],
  });
  await sleep(500);
  return result.result;
}

async function cmdNewTab({ url }) {
  const tab = await chrome.tabs.create({ url: url || "about:blank", active: true });
  if (url) {
    // Wait for page load
    await new Promise((resolve) => {
      const listener = (tabId, info) => {
        if (tabId === tab.id && info.status === "complete") {
          chrome.tabs.onUpdated.removeListener(listener);
          resolve();
        }
      };
      chrome.tabs.onUpdated.addListener(listener);
      setTimeout(() => {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }, 15_000);
    });
    await sleep(500);
    injectOverlay(tab.id);
  }
  return { ok: true, tabId: tab.id };
}

async function cmdSwitchTab({ index }) {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  if (index < 0 || index >= tabs.length) {
    return { error: `tab index ${index} out of range (0-${tabs.length - 1})` };
  }
  await chrome.tabs.update(tabs[index].id, { active: true });
  await sleep(300);
  const tab = tabs[index];
  return { ok: true, url: tab.url, title: tab.title };
}

async function cmdCloseTab() {
  const tab = await getActiveTab();
  if (!tab) return { error: "no active tab" };
  const tabs = await chrome.tabs.query({ currentWindow: true });
  if (tabs.length <= 1) return { error: "cannot close the last tab" };
  await chrome.tabs.remove(tab.id);
  await sleep(300);
  return { ok: true };
}

async function cmdGetTabs() {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const activeTab = await getActiveTab();
  return {
    tabs: tabs.map((t, i) => ({
      index: i,
      title: t.title,
      url: t.url,
      active: t.id === activeTab?.id,
    })),
  };
}

async function cmdGetUrl() {
  const tab = await getActiveTab();
  return { url: tab.url, title: tab.title };
}

// --- Helpers ---

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForPageSettle(tabId, timeout = 2000) {
  // Wait for SPA content to load after an action:
  // Observe DOM mutations — if DOM is still changing, wait until it stabilizes.
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      func: (timeoutMs) => {
        return new Promise((resolve) => {
          let timer = null;
          let settled = false;
          const observer = new MutationObserver(() => {
            // Reset timer on each mutation — DOM is still changing
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => {
              settled = true;
              observer.disconnect();
              resolve();
            }, 300); // 300ms of no mutations = settled
          });
          observer.observe(document.body || document.documentElement, {
            childList: true,
            subtree: true,
            attributes: true,
          });
          // Start initial timer
          timer = setTimeout(() => {
            if (!settled) {
              observer.disconnect();
              resolve();
            }
          }, 300);
          // Hard timeout
          setTimeout(() => {
            observer.disconnect();
            resolve();
          }, timeoutMs);
        });
      },
      args: [timeout],
    });
  } catch {
    // Page might have navigated away — that's fine
    await sleep(500);
  }
}

async function cmdZoom({ level }) {
  const tab = await getActiveTab();
  // level is a percentage: 100 = normal, 200 = 2x, 50 = half
  const factor = (level || 100) / 100;
  await chrome.tabs.setZoom(tab.id, factor);
  await sleep(300);
  return { ok: true, zoom: level };
}

async function cmdResolve({ selector }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (sel) => {
      function tryQ(s) { try { return document.querySelector(s) ? s : null; } catch { return null; } }

      // 1. Exact match
      if (tryQ(sel)) return { selector: sel };

      // 2. Fix space-separated classes → dot-separated (.foo bar → .foo.bar)
      const dotFixed = sel.replace(/\.([A-Za-z0-9_-]+)\s+([A-Za-z])/g, ".$1.$2");
      if (dotFixed !== sel && tryQ(dotFixed)) return { selector: dotFixed };

      // 3. Attribute exact match → starts-with (handles trailing spaces/extra chars)
      const startsWith = sel.replace(/\[([^\]=*^~|]+)=['"]([^'"]+)['"]\]/g, '[$1^="$2"]');
      if (startsWith !== sel && tryQ(startsWith)) return { selector: startsWith };

      // 4. Attribute exact match → contains
      const contains = sel.replace(/\[([^\]=*^~|]+)=['"]([^'"]+)['"]\]/g, '[$1*="$2"]');
      if (contains !== sel && tryQ(contains)) return { selector: contains };

      // 5. Strip :nth-of-type and retry (LLM sometimes gets index wrong)
      const noNth = sel.replace(/:nth-of-type\(\d+\)/g, "");
      if (noNth !== sel && tryQ(noNth)) return { selector: noNth };

      return { error: "element not found" };
    },
    args: [selector],
  });
  return result.result;
}

// --- DOM extraction function (injected into pages) ---
// Returns clean viewport HTML with text truncation

function extractDomFromPage() {
  const MAX_TEXT = 200;
  const MAX_HTML = 30000; // chars

  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "SVG", "NOSCRIPT", "META", "LINK", "TEMPLATE",
    "IFRAME", "OBJECT", "EMBED", "PATH", "DEFS", "CLIPPATH", "BR",
  ]);
  const KEEP_ATTRS = new Set([
    "id", "class", "href", "src", "type", "name", "placeholder", "value",
    "role", "aria-label", "aria-expanded", "aria-selected", "aria-checked",
    "checked", "disabled", "readonly", "for", "action", "method", "alt", "title",
    "data-testid", "data-test-id", "data-cy", "data-id",
  ]);

  // --- Build clean HTML (viewport-only) ---

  let htmlSize = 0;
  const vpH = window.innerHeight;
  const vpW = window.innerWidth;
  const VP_MARGIN = 50; // small margin to catch elements near edges

  function isInViewport(el) {
    try {
      const rect = el.getBoundingClientRect();
      // Skip elements completely outside the viewport
      if (rect.bottom < -VP_MARGIN || rect.top > vpH + VP_MARGIN) return false;
      if (rect.right < -VP_MARGIN || rect.left > vpW + VP_MARGIN) return false;
      // Skip zero-size elements
      if (rect.width <= 0 && rect.height <= 0) return false;
      return true;
    } catch {
      return true; // if we can't check, include it
    }
  }

  function isElVisible(el) {
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  function escHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function processNode(node) {
    if (htmlSize > MAX_HTML) return "";

    if (node.nodeType === Node.TEXT_NODE) {
      let text = node.textContent;
      text = text.replace(/\s+/g, " ");
      if (!text.trim()) return "";
      if (text.length > MAX_TEXT) text = text.substring(0, MAX_TEXT) + "...";
      htmlSize += text.length;
      return escHtml(text);
    }

    if (node.nodeType !== Node.ELEMENT_NODE) return "";

    const el = node;
    const tag = el.tagName;

    if (SKIP_TAGS.has(tag)) return "";
    if (el.id === "__ghost_overlay") return "";
    if (el.getAttribute("aria-hidden") === "true") return "";

    try { if (!isElVisible(el)) return ""; } catch {}

    // Skip elements outside the viewport (only show what's in the screenshot)
    // Allow BODY/HTML and major structural tags to pass through so the tree stays valid
    const STRUCTURAL = new Set(["BODY", "HTML", "MAIN", "FORM"]);
    if (!STRUCTURAL.has(tag) && !isInViewport(el)) return "";

    const tagL = tag.toLowerCase();
    let attrs = "";

    for (const attr of el.attributes) {
      if (KEEP_ATTRS.has(attr.name)) {
        let val = attr.value;

        // Filter obfuscated class names — they confuse the LLM
        if (attr.name === "class") {
          const clean = val.split(/\s+/).filter(c => {
            if (c.length < 2) return false;
            // Keep classes with separators (nav-link, btn_primary)
            if (c.includes("-") || c.includes("_")) return true;
            // Keep all-lowercase classes (header, sidebar, active)
            if (c === c.toLowerCase() && c.length >= 3) return true;
            // Drop mixed-case without separators (BLohnc, GYgkab = obfuscated)
            return false;
          });
          if (clean.length === 0) continue; // skip class attr entirely
          val = clean.join(" ");
        }

        if (val.length > MAX_TEXT) val = val.substring(0, MAX_TEXT) + "...";
        attrs += ` ${attr.name}="${escHtml(val)}"`;
      }
    }

    if (["INPUT", "IMG", "HR"].includes(tag)) {
      const html = `<${tagL}${attrs}>`;
      htmlSize += html.length;
      return html;
    }

    let children = "";
    for (const child of el.childNodes) {
      children += processNode(child);
    }

    // Skip empty wrappers, unwrap wrappers with no meaningful attrs
    const WRAPPER_TAGS = new Set([
      "DIV", "SPAN", "SECTION", "ARTICLE", "HEADER", "FOOTER",
      "MAIN", "NAV", "ASIDE", "UL", "OL", "LI", "P", "FIGURE",
    ]);
    if (WRAPPER_TAGS.has(tag)) {
      if (!children.trim()) return "";
      if (!attrs.trim()) return children;
    }

    const html = `<${tagL}${attrs}>${children}</${tagL}>`;
    htmlSize += tagL.length * 2 + 5;
    return html;
  }

  const html = processNode(document.body || document.documentElement);

  return {
    html,
    url: location.href,
    title: document.title,
    scroll: {
      top: Math.round(window.scrollY),
      height: Math.round(document.documentElement.scrollHeight),
      viewport: Math.round(window.innerHeight),
    },
  };
}

// --- Automation overlay — animated blue glow border ---

function injectOverlay(tabId) {
  chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func: () => {
      if (document.getElementById("__ghost_overlay")) return;

      const overlay = document.createElement("div");
      overlay.id = "__ghost_overlay";

      const style = document.createElement("style");
      style.textContent = `
        @keyframes __ghost_pulse {
          0%, 100% {
            box-shadow:
              inset 0 0 0 3px rgba(59, 130, 246, 0.6),
              inset 0 0 18px rgba(59, 130, 246, 0.2),
              0 0 12px rgba(59, 130, 246, 0.15);
          }
          50% {
            box-shadow:
              inset 0 0 0 3px rgba(99, 160, 255, 1),
              inset 0 0 30px rgba(99, 160, 255, 0.35),
              0 0 24px rgba(99, 160, 255, 0.25);
          }
        }
        #__ghost_overlay {
          position: fixed;
          inset: 0;
          z-index: 2147483647;
          pointer-events: none;
          border-radius: 12px;
          animation: __ghost_pulse 2s ease-in-out infinite;
        }
      `;

      document.documentElement.appendChild(style);
      document.documentElement.appendChild(overlay);
    },
  }).catch(() => {});
}

// Inject overlay on every page load
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "complete") {
    injectOverlay(tabId);
  }
});

// --- Start ---
connect();
