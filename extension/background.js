// Ghost Extension Bridge — WebSocket client + command router
//
// Paradigm: the agent perceives the page as a numbered list of interactive
// elements (text outline + matching badges on the screenshot) and acts by
// index. No CSS selectors, no text matching — language-agnostic by design.

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
      case "observe":
        return await cmdObserve();
      case "act_index":
        return await cmdActIndex(params);
      case "scroll":
        return await cmdScroll(params);
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
      case "zoom":
        return await cmdZoom(params);
      default:
        return { error: `unknown command: ${command}` };
    }
  } catch (err) {
    return { error: err.message || "Unknown command error" };
  }
}

// --- Element registry (worker side) ---
// Built by cmdObserve, consumed by cmdActIndex. Element references live in
// each frame's ISOLATED world (window.__ghostEls); the worker only keeps the
// index → frame mapping.

let ghostRegistry = null; // { obsSeq, tabId, map: {gi: {frameId, localIndex}}, labels: {gi: name} }
let obsSeqCounter = 0;

function clearRegistry() {
  ghostRegistry = null;
}

// --- observe: extract + badge + screenshot in ONE round trip ---

async function cmdObserve() {
  const tab = await getActiveTab();
  if (!tab) return { error: "no active tab" };
  const url = tab.url || "";
  if (!url || url === "about:blank" || url.startsWith("chrome://")
      || url.startsWith("chrome-extension://") || url.startsWith("about:")) {
    return { outline: "", image: "", url, title: tab.title || "", error: "internal page" };
  }

  const obsSeq = ++obsSeqCounter;
  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      world: "ISOLATED",
      func: observeFrame,
      args: [obsSeq],
    });
  } catch (err) {
    return { error: err.message };
  }

  // Top frame (frameId 0) first; drop frames that returned nothing useful
  const frames = results
    .filter((r) => r && r.result && (r.result.elements.length || (r.result.outline || "").trim()))
    .sort((a, b) => (a.frameId === 0 ? -1 : b.frameId === 0 ? 1 : 0));
  const top = frames.find((f) => f.frameId === 0)?.result || null;

  // Badge offsets: child frame's absolute position = parent offset + its
  // <iframe> rect in the parent, matched by URL. Ambiguous/unmatched → null
  // (elements stay usable through the text outline, just unbadged).
  const offsets = { 0: { x: 0, y: 0 } };
  let progressed = true;
  while (progressed) {
    progressed = false;
    for (const f of frames) {
      if (f.frameId in offsets) continue;
      for (const p of frames) {
        const po = offsets[p.frameId];
        if (po === undefined || po === null) continue;
        const cands = (p.result.iframes || []).filter((ifr) => frameUrlMatch(ifr.url, f.result.url));
        if (cands.length === 1) {
          offsets[f.frameId] = { x: po.x + cands[0].rect.x, y: po.y + cands[0].rect.y };
          progressed = true;
          break;
        } else if (cands.length > 1) {
          offsets[f.frameId] = null;
          progressed = true;
          break;
        }
      }
    }
  }

  // Stitch: global indices, combined outline, registry, badge list
  const map = {};
  const labels = {};
  const badges = [];
  const sections = [];
  let offset = 0;
  let anyTruncated = false;
  const vpW = top?.scroll?.viewportW || 100000;
  const vpH = top?.scroll?.viewport || 100000;

  for (const f of frames) {
    const r = f.result;
    anyTruncated = anyTruncated || r.truncated;
    const fo = f.frameId in offsets ? offsets[f.frameId] : null;
    const renumbered = (r.outline || "").replace(
      /\[\[(\d+)\]\]/g,
      (_, n) => `[${offset + Number(n)}]`,
    );
    for (const e of r.elements) {
      const gi = offset + e.i;
      map[gi] = { frameId: f.frameId, localIndex: e.i };
      labels[gi] = e.name || e.tag;
      if (fo && e.rect) {
        const bx = fo.x + e.rect.x;
        const by = fo.y + e.rect.y;
        if (bx > -10 && by > -10 && bx < vpW + 10 && by < vpH + 10) {
          badges.push({ index: gi, x: bx, y: by });
        }
      }
    }
    if (f.frameId === 0) {
      sections.push(renumbered);
    } else {
      const note = fo ? "" : " (no badges on screenshot)";
      sections.push(`--- frame${note}: ${r.url} ---\n${renumbered}`);
    }
    offset += r.elements.length;
  }

  let header = "";
  if (top) {
    const s = top.scroll;
    const pct = s.height <= s.viewport ? 100 : Math.round(((s.top + s.viewport) / s.height) * 100);
    const hint = pct < 95 ? "more content below" : "near bottom";
    header = `Page: ${top.title}\nURL: ${top.url}\nScroll: ${pct}% (${hint})\n`
      + "Interactive elements are numbered [N]; the same numbers appear as red badges on the screenshot.\n\n";
  }
  let outline = header + sections.join("\n");
  if (anyTruncated) {
    outline += "\n[OUTLINE TRUNCATED — scroll or extract_text to reveal more]";
  }

  // Set-of-marks screenshot: paint badges, capture, always remove
  let image = "";
  try {
    if (badges.length) await paintBadges(tab.id, badges);
    image = await captureAndDownscale(tab.windowId);
  } catch {
    image = "";
  } finally {
    await removeBadges(tab.id);
  }

  ghostRegistry = { obsSeq, tabId: tab.id, map, labels };

  return {
    outline,
    image,
    url: top?.url || url,
    title: top?.title || tab.title || "",
    scroll: top?.scroll || null,
    labels,
    elementCount: offset,
    truncated: anyTruncated,
    obsSeq,
  };
}

function frameUrlMatch(iframeSrc, frameHref) {
  if (!iframeSrc || !frameHref) return false;
  if (iframeSrc === frameHref) return true;
  try {
    return new URL(iframeSrc, location?.href).origin === new URL(frameHref).origin;
  } catch {
    return false;
  }
}

async function paintBadges(tabId, badgeList) {
  await chrome.scripting.executeScript({
    target: { tabId }, // top frame only — positions are pre-computed absolute
    world: "ISOLATED",
    func: (list) => {
      document.getElementById("__ghost_marks")?.remove(); // clear leftovers
      const c = document.createElement("div");
      c.id = "__ghost_marks";
      c.style.cssText = "position:fixed;inset:0;z-index:2147483646;pointer-events:none;";
      for (const b of list) {
        const d = document.createElement("div");
        d.textContent = b.index;
        d.style.cssText =
          `position:absolute;left:${b.x}px;top:${b.y}px;` +
          "background:#e11;color:#fff;font:bold 11px/1.3 monospace;" +
          "padding:0 3px;border-radius:3px;transform:translate(-2px,-100%);";
        c.appendChild(d);
      }
      document.documentElement.appendChild(c);
    },
    args: [badgeList],
  });
}

function removeBadges(tabId) {
  return chrome.scripting.executeScript({
    target: { tabId },
    world: "ISOLATED",
    func: () => document.getElementById("__ghost_marks")?.remove(),
  }).catch(() => {});
}

// Injected into EVERY frame. Fully self-contained (MV3 serialization).
// Detects interactive elements by BEHAVIOR (not tag), stores live references
// in this frame's isolated world, and returns a text outline where element
// text passes through verbatim in whatever language the page uses.
function observeFrame(obsSeq) {
  const isTop = window === window.top;
  const MAX_OUTLINE = isTop ? 20000 : 4000;
  const MAX_NAME = 80;
  const MAX_TEXT_LINE = 400;
  const VP_MARGIN = 50;
  const vpH = window.innerHeight;
  const vpW = window.innerWidth;

  const INTERACTIVE_TAGS = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"]);
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "option",
    "tab", "checkbox", "radio", "switch", "combobox", "listbox", "textbox",
    "searchbox", "slider", "spinbutton", "treeitem",
  ]);
  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "SVG", "NOSCRIPT", "META", "LINK", "TEMPLATE",
    "OBJECT", "EMBED", "PATH", "DEFS", "CLIPPATH", "BR",
  ]);
  const STRUCTURAL = new Set(["BODY", "HTML", "MAIN", "FORM", "HEADER", "FOOTER", "NAV"]);

  const els = [];     // live element refs — index is the local index
  const meta = [];    // JSON-serializable mirror of els
  const iframes = [];
  const lines = [];
  let outLen = 0;
  let truncated = false;
  let textBuf = "";

  function collapse(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  function flushText() {
    let t = collapse(textBuf);
    textBuf = "";
    if (!t) return;
    if (t.length > MAX_TEXT_LINE) t = t.slice(0, MAX_TEXT_LINE) + "…";
    lines.push(t);
    outLen += t.length;
  }

  function inViewport(rect) {
    if (rect.bottom < -VP_MARGIN || rect.top > vpH + VP_MARGIN) return false;
    if (rect.right < -VP_MARGIN || rect.left > vpW + VP_MARGIN) return false;
    if (rect.width <= 0 && rect.height <= 0) return false;
    return true;
  }

  function isHardInteractive(el, role) {
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (el.tagName === "LABEL") return true;
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return true;
    return false;
  }

  function isSoftInteractive(el, style) {
    if (el.hasAttribute("onclick") || el.hasAttribute("jsaction")) return true;
    if (el.hasAttribute("tabindex") && el.tabIndex >= 0) return true;
    if (style.cursor === "pointer") return true;
    return false;
  }

  function accName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return collapse(aria).slice(0, MAX_NAME);
    const t = collapse(el.innerText);
    if (t) return t.slice(0, MAX_NAME);
    return collapse(
      el.getAttribute("alt") || el.getAttribute("title")
      || el.getAttribute("placeholder") || el.value || "",
    ).slice(0, MAX_NAME);
  }

  function attrStr(el, role) {
    let s = "";
    const add = (k, v) => {
      if (v === null || v === undefined || v === "" || v === false) return;
      let sv = String(v);
      if (sv.length > 60) sv = sv.slice(0, 60) + "…";
      s += ` ${k}=${sv}`;
    };
    if (role) add("role", role);
    add("type", el.getAttribute("type"));
    add("name", el.getAttribute("name"));
    add("placeholder", el.getAttribute("placeholder"));
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      add("value", (el.value || "").slice(0, 40));
    }
    if (el.checked === true) add("checked", "true");
    if (el.disabled === true) add("disabled", "true");
    add("aria-expanded", el.getAttribute("aria-expanded"));
    add("aria-selected", el.getAttribute("aria-selected"));
    let href = el.getAttribute("href");
    if (href) add("href", href.startsWith("data:") ? "[data-uri]" : href);
    return s;
  }

  function registerInteractive(el, role) {
    const idx = els.length;
    els.push(el);
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const name = accName(el);
    meta.push({
      i: idx,
      tag,
      role: role || null,
      name,
      rect: {
        x: Math.round(rect.left), y: Math.round(rect.top),
        w: Math.round(rect.width), h: Math.round(rect.height),
      },
    });
    const line = `[[${idx}]]<${tag}${attrStr(el, role)}>${name}</${tag}>`;
    lines.push(line);
    outLen += line.length;
  }

  function walk(node, insideInteractive) {
    if (outLen > MAX_OUTLINE) {
      truncated = true;
      return;
    }

    if (node.nodeType === Node.TEXT_NODE) {
      if (!insideInteractive) textBuf += " " + node.textContent;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    const el = node;
    const tag = el.tagName;
    if (SKIP_TAGS.has(tag)) return;
    if (el.id === "__ghost_overlay" || el.id === "__ghost_marks") return;
    if (el.getAttribute("aria-hidden") === "true") return;

    let style;
    try {
      style = getComputedStyle(el);
    } catch {
      return;
    }
    if (style.display === "none" || style.visibility === "hidden") return;

    const rect = el.getBoundingClientRect();
    if (!STRUCTURAL.has(tag) && !inViewport(rect)) return;

    if (tag === "IFRAME") {
      // Content arrives from the frame's own injection; record position so
      // the worker can place its badges and label its outline section
      if (el.src && rect.width > 0 && rect.height > 0) {
        iframes.push({
          url: el.src,
          rect: {
            x: Math.round(rect.left), y: Math.round(rect.top),
            w: Math.round(rect.width), h: Math.round(rect.height),
          },
        });
      }
      return;
    }

    const role = el.getAttribute("role");
    const hard = isHardInteractive(el, role);
    // Soft interactivity (cursor/tabindex/onclick) inherits visually — only
    // the OUTERMOST soft element registers; hard ones always do
    const interactive = hard || (!insideInteractive && isSoftInteractive(el, style));

    if (interactive) {
      flushText();
      registerInteractive(el, role);
      // Descend only to find nested hard interactives (text is already
      // summarized in the element's name)
      for (const child of (el.shadowRoot || el).childNodes) {
        walk(child, true);
      }
      return;
    }

    if (insideInteractive) {
      // Inside an interactive element we only look for nested hard interactives
      for (const child of (el.shadowRoot || el).childNodes) {
        walk(child, true);
      }
      return;
    }

    const isBlock = !style.display.startsWith("inline") && style.display !== "contents";
    if (isBlock) flushText();
    for (const child of (el.shadowRoot || el).childNodes) {
      walk(child, false);
    }
    if (isBlock) flushText();
  }

  try {
    walk(document.body || document.documentElement, false);
    flushText();
  } catch {
    // partial results are fine
  }

  window.__ghostEls = els;
  window.__ghostObsSeq = obsSeq;

  return {
    url: location.href,
    title: document.title,
    isTop,
    scroll: {
      top: Math.round(window.scrollY),
      height: Math.round(document.documentElement.scrollHeight),
      viewport: vpH,
      viewportW: vpW,
    },
    elements: meta,
    outline: lines.join("\n"),
    iframes,
    truncated,
  };
}

// --- act by index ---

// Actions that can trigger navigation (executeScript may reject mid-flight)
// and deserve a settle wait afterwards.
const NAV_ACTIONS = new Set(["click", "double_click", "press_key", "select", "drag_drop"]);

async function cmdActIndex(params) {
  const tab = await getActiveTab();
  const action = params.action;
  const hasIndex = params.index !== undefined && params.index !== null;

  // Global press_key (no target element) works without a registry
  if (!(action === "press_key" && !hasIndex)) {
    if (!ghostRegistry || ghostRegistry.tabId !== tab.id
        || ghostRegistry.obsSeq !== params.obsSeq) {
      return { error: "stale observation — page changed, re-observe" };
    }
  }

  let frameId = 0;
  let localIndex = null;
  let toLocalIndex = null;
  if (hasIndex) {
    const m = ghostRegistry.map[params.index];
    if (!m) return { error: `unknown index ${params.index} — re-observe` };
    frameId = m.frameId;
    localIndex = m.localIndex;
  }
  if (action === "drag_drop") {
    const mt = ghostRegistry?.map[params.to_index];
    if (!mt) return { error: `unknown index ${params.to_index} — re-observe` };
    if (mt.frameId !== frameId) {
      return { error: "drag between different frames is not supported" };
    }
    toLocalIndex = mt.localIndex;
  }

  let result;
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, frameIds: [frameId] },
      world: "ISOLATED",
      func: actByIndex,
      args: [action, localIndex, {
        text: params.text ?? null,
        value: params.value ?? null,
        key: params.key ?? null,
        direction: params.direction ?? null,
        toLocalIndex,
      }, params.obsSeq ?? null],
    });
    result = r.result;
  } catch (err) {
    if (NAV_ACTIONS.has(action)) {
      // Page navigated away mid-action (form submit, link click) — success
      result = { ok: true, navigated: true };
    } else {
      return { error: err.message };
    }
  }

  if (result && result.error) return result;
  if (NAV_ACTIONS.has(action)) {
    await waitForPageSettle(tab.id);
  } else if (action === "hover") {
    await sleep(200); // let tooltips/dropdowns appear
  }
  return result;
}

// Injected into the element's frame. Self-contained (MV3 serialization).
function actByIndex(action, localIndex, args, obsSeq) {
  let el = null;
  if (localIndex !== null) {
    if (window.__ghostObsSeq !== obsSeq || !window.__ghostEls) {
      return { error: "element registry is stale (frame reloaded) — re-observe" };
    }
    el = window.__ghostEls[localIndex];
    if (!el || !el.isConnected) {
      return { error: "element no longer on page — re-observe" };
    }
  }

  function label(node) {
    if (!node) return "";
    let t = node.tagName.toLowerCase();
    if (node.id) t += "#" + node.id;
    return t;
  }

  function center(node) {
    const rect = node.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  // Full pointer/mouse event sequence (critical for SPAs like React/Vue)
  function mouseSeq(node, mode) {
    node.scrollIntoView({ block: "center", behavior: "instant" });
    const { x, y } = center(node);
    const button = mode === "right" ? 2 : 0;
    const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button };
    const pe = (type) => node.dispatchEvent(new PointerEvent(type, { ...opts, pointerType: "mouse" }));
    const me = (type, extra) => node.dispatchEvent(new MouseEvent(type, { ...opts, ...(extra || {}) }));
    pe("pointerover"); me("mouseover");
    pe("pointerenter"); me("mouseenter");
    if (mode === "hover") { pe("pointermove"); me("mousemove"); return; }
    pe("pointerdown"); me("mousedown");
    if (mode !== "right") node.focus();
    pe("pointerup"); me("mouseup");
    if (mode === "right") { me("contextmenu", { button: 2 }); return; }
    me("click");
    if (mode === "double") {
      pe("pointerdown"); me("mousedown", { detail: 2 });
      pe("pointerup"); me("mouseup", { detail: 2 });
      me("click", { detail: 2 });
      me("dblclick", { detail: 2 });
    }
  }

  function typeInto(node, txt, clr) {
    node.scrollIntoView({ block: "center", behavior: "instant" });
    node.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    node.focus();
    node.dispatchEvent(new FocusEvent("focus", { bubbles: false }));

    if (node.isContentEditable) {
      if (clr !== false) {
        document.execCommand("selectAll", false, null);
        document.execCommand("delete", false, null);
      }
      if (!document.execCommand("insertText", false, txt)) {
        node.textContent = (clr === false ? node.textContent : "") + txt;
        node.dispatchEvent(new InputEvent("input", { bubbles: true, data: txt, inputType: "insertText" }));
      }
      node.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    // Native value setter bypasses React's synthetic event system — compute once
    const nativeSetter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), "value")?.set
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    const setVal = (v) => { if (nativeSetter) nativeSetter.call(node, v); else node.value = v; };

    if (clr !== false) {
      setVal("");
      node.dispatchEvent(new Event("input", { bubbles: true }));
    }
    for (const ch of txt) {
      // Unicode-safe: `key` carries the character in any script; `code` only
      // exists for Latin letters/digits and degrades gracefully otherwise
      const code = /^[a-zA-Z]$/.test(ch) ? "Key" + ch.toUpperCase()
        : /^[0-9]$/.test(ch) ? "Digit" + ch
        : "";
      node.dispatchEvent(new KeyboardEvent("keydown", { key: ch, code, bubbles: true }));
      node.dispatchEvent(new KeyboardEvent("keypress", { key: ch, code, bubbles: true }));
      setVal(node.value + ch);
      node.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
      node.dispatchEvent(new KeyboardEvent("keyup", { key: ch, code, bubbles: true }));
    }
    node.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Language-agnostic: match against the page's own option strings (value or
  // visible label), never against hardcoded words
  function selectOption(node, wanted) {
    node.scrollIntoView({ block: "center", behavior: "instant" });
    node.focus();
    const w = String(wanted ?? "");
    const opts = Array.from(node.options || []);
    const opt = opts.find((o) => o.value === w)
      || opts.find((o) => (o.label || o.textContent || "").trim() === w.trim())
      || opts.find((o) => (o.label || o.textContent || "").trim().toLowerCase() === w.trim().toLowerCase());
    if (!opt) {
      const available = opts.slice(0, 20).map((o) => (o.label || o.textContent || "").trim()).join(" | ");
      return { error: `no option matches "${w}" — options: ${available}` };
    }
    const nativeSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (nativeSetter) nativeSetter.call(node, opt.value); else node.value = opt.value;
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
    return null;
  }

  function pressKey(target, k, hasTarget) {
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
    if (hasTarget) {
      target.scrollIntoView({ block: "center", behavior: "instant" });
      target.focus();
    }
    const mapped = KEY_MAP[String(k).toLowerCase()]
      || { key: k, code: /^[a-zA-Z]$/.test(k) ? "Key" + k.toUpperCase() : "", keyCode: String(k).charCodeAt(0) };
    const opts = {
      key: mapped.key, code: mapped.code, keyCode: mapped.keyCode,
      which: mapped.keyCode, bubbles: true, cancelable: true,
    };
    target.dispatchEvent(new KeyboardEvent("keydown", opts));
    target.dispatchEvent(new KeyboardEvent("keypress", opts));
    target.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (mapped.key === "Enter" && target.form) {
      target.form.requestSubmit?.() || target.form.submit();
    }
    return { ok: true, target: target.tagName?.toLowerCase() };
  }

  function dragDrop(from, to) {
    from.scrollIntoView({ block: "center", behavior: "instant" });
    const fr = from.getBoundingClientRect();
    const tr = to.getBoundingClientRect();
    const fx = fr.left + fr.width / 2, fy = fr.top + fr.height / 2;
    const tx = tr.left + tr.width / 2, ty = tr.top + tr.height / 2;
    const dataTransfer = new DataTransfer();
    const de = (target, type, cx, cy) => target.dispatchEvent(new DragEvent(type, {
      bubbles: true, cancelable: true, clientX: cx, clientY: cy, dataTransfer,
    }));
    from.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, cancelable: true, clientX: fx, clientY: fy, pointerType: "mouse",
    }));
    from.dispatchEvent(new MouseEvent("mousedown", {
      bubbles: true, cancelable: true, clientX: fx, clientY: fy,
    }));
    de(from, "dragstart", fx, fy);
    de(from, "drag", fx, fy);
    for (let i = 1; i <= 5; i++) {
      const cx = fx + (tx - fx) * (i / 5);
      const cy = fy + (ty - fy) * (i / 5);
      const over = document.elementFromPoint(cx, cy);
      if (over) de(over, "dragover", cx, cy);
    }
    de(to, "dragenter", tx, ty);
    de(to, "dragover", tx, ty);
    de(to, "drop", tx, ty);
    de(from, "dragend", tx, ty);
    to.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, cancelable: true, clientX: tx, clientY: ty, pointerType: "mouse",
    }));
    to.dispatchEvent(new MouseEvent("mouseup", {
      bubbles: true, cancelable: true, clientX: tx, clientY: ty,
    }));
  }

  switch (action) {
    case "click":
      mouseSeq(el, "click");
      return { ok: true, resolved: label(el), tag: el.tagName.toLowerCase() };
    case "double_click":
      mouseSeq(el, "double");
      return { ok: true, resolved: label(el) };
    case "right_click":
      mouseSeq(el, "right");
      return { ok: true, resolved: label(el) };
    case "hover":
      mouseSeq(el, "hover");
      return { ok: true, resolved: label(el) };
    case "type":
      typeInto(el, args.text ?? "", true);
      return { ok: true, resolved: label(el) };
    case "select": {
      const err = selectOption(el, args.value);
      return err || { ok: true, resolved: label(el) };
    }
    case "press_key":
      return pressKey(el || document.activeElement || document.body, args.key || "Enter", !!el);
    case "extract_text": {
      const text = (el.innerText || el.textContent || "").trim();
      return { ok: true, resolved: label(el), text: text.slice(0, 4000) };
    }
    case "scroll": {
      // Scroll the element's own scrollable container (falls back to page)
      const scrollable = (n) => n && n.scrollHeight > n.clientHeight + 4
        && /(auto|scroll|overlay)/.test(getComputedStyle(n).overflowY);
      let container = el;
      while (container && !scrollable(container)) container = container.parentElement;
      if (!container) container = document.scrollingElement || document.documentElement;
      const amount = (container.clientHeight || window.innerHeight) * 0.7;
      container.scrollBy({ top: args.direction === "up" ? -amount : amount, behavior: "instant" });
      return { ok: true, resolved: label(container) };
    }
    case "drag_drop": {
      const to = window.__ghostEls[args.toLocalIndex];
      if (!to || !to.isConnected) {
        return { error: "target element no longer on page — re-observe" };
      }
      dragDrop(el, to);
      return { ok: true };
    }
    default:
      return { error: `unknown act action: ${action}` };
  }
}

// --- Commands ---

async function cmdNavigate({ url }) {
  const tab = await getActiveTab();
  clearRegistry();
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
  await sleep(200);
  injectOverlay(tab.id);
  return { ok: true };
}

async function cmdScroll({ direction }) {
  const tab = await getActiveTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "ISOLATED",
    func: (dir) => {
      const amount = window.innerHeight * 0.7;
      const delta = dir === "up" ? -amount : amount;
      const scrollable = document.scrollingElement || document.documentElement;
      scrollable.scrollBy({ top: delta, behavior: "instant" });
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
    args: [direction],
  });
  // Brief pause so lazy-loaded content below the fold can start rendering
  await sleep(100);
  return result.result;
}

async function cmdScreenshot() {
  const tab = await getActiveTab();
  if (!tab) return { error: "no active tab" };

  const url = tab.url || "";
  if (!url || url === "about:blank" || url.startsWith("chrome://")
      || url.startsWith("chrome-extension://") || url.startsWith("about:")) {
    return { error: "cannot capture internal/blank pages" };
  }

  try {
    const image = await captureAndDownscale(tab.windowId);
    return { image };
  } catch (err) {
    return { error: err.message };
  }
}

// Shared capture pipeline: full retina capture is ~10x bigger than anything
// the LLM or scenario files need, so downscale in the worker
async function captureAndDownscale(windowId) {
  const dataUrl = await chrome.tabs.captureVisibleTab(windowId, {
    format: "jpeg",
    quality: 80,
  });
  const MAX_W = 1280;
  const blob = await (await fetch(dataUrl)).blob();
  const bmp = await createImageBitmap(blob);
  if (bmp.width <= MAX_W) {
    bmp.close();
    return dataUrl.replace(/^data:image\/\w+;base64,/, "");
  }
  const scale = MAX_W / bmp.width;
  const canvas = new OffscreenCanvas(Math.round(bmp.width * scale), Math.round(bmp.height * scale));
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  const out = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.7 });
  const buf = new Uint8Array(await out.arrayBuffer());
  // Chunked btoa — String.fromCharCode on the whole buffer blows the stack
  let binary = "";
  for (let i = 0; i < buf.length; i += 8192) {
    binary += String.fromCharCode.apply(null, buf.subarray(i, i + 8192));
  }
  return btoa(binary);
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
  return historyNav("back");
}

async function cmdForward() {
  return historyNav("forward");
}

async function historyNav(direction) {
  const tab = await getActiveTab();
  clearRegistry();
  try {
    if (direction === "back") await chrome.tabs.goBack(tab.id);
    else await chrome.tabs.goForward(tab.id);
  } catch {
    return { ok: true, note: "no further history" };
  }
  // Wait for the load to complete instead of a blind sleep
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
    }, 5000);
  });
  await waitForPageSettle(tab.id);
  return { ok: true };
}

async function cmdNewTab({ url }) {
  clearRegistry();
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
    await sleep(200);
    injectOverlay(tab.id);
  }
  return { ok: true, tabId: tab.id };
}

async function cmdSwitchTab({ index }) {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  if (index < 0 || index >= tabs.length) {
    return { error: `tab index ${index} out of range (0-${tabs.length - 1})` };
  }
  clearRegistry();
  await chrome.tabs.update(tabs[index].id, { active: true });
  await sleep(150);
  const tab = tabs[index];
  return { ok: true, url: tab.url, title: tab.title };
}

async function cmdCloseTab() {
  const tab = await getActiveTab();
  if (!tab) return { error: "no active tab" };
  const tabs = await chrome.tabs.query({ currentWindow: true });
  if (tabs.length <= 1) return { error: "cannot close the last tab" };
  clearRegistry();
  await chrome.tabs.remove(tab.id);
  await sleep(150);
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

async function waitForPageSettle(tabId, timeout = 1500) {
  // Wait for SPA content to load after an action:
  // Observe DOM mutations — if DOM is still changing, wait until it stabilizes.
  // childList-only: watching attributes would let spinners/animations reset
  // the debounce forever and always burn the hard timeout.
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      func: (timeoutMs) => {
        return new Promise((resolve) => {
          const DEBOUNCE = 150; // ms of no mutations = settled
          let timer = null;
          const finish = () => {
            observer.disconnect();
            resolve();
          };
          const observer = new MutationObserver(() => {
            if (timer) clearTimeout(timer);
            timer = setTimeout(finish, DEBOUNCE);
          });
          observer.observe(document.body || document.documentElement, {
            childList: true,
            subtree: true,
          });
          timer = setTimeout(finish, DEBOUNCE);
          setTimeout(finish, timeoutMs); // hard timeout
        });
      },
      args: [timeout],
    });
  } catch {
    // Page might have navigated away — that's fine
    await sleep(300);
  }
}

async function cmdZoom({ level }) {
  const tab = await getActiveTab();
  // level is a percentage: 100 = normal, 200 = 2x, 50 = half
  const factor = (level || 100) / 100;
  await chrome.tabs.setZoom(tab.id, factor);
  await sleep(150);
  return { ok: true, zoom: level };
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

// Inject overlay on every page load; drop the element registry the moment a
// page starts loading (its element references die with the old document)
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && ghostRegistry && ghostRegistry.tabId === tabId) {
    clearRegistry();
  }
  if (info.status === "complete") {
    injectOverlay(tabId);
  }
});

// --- Start ---
connect();
