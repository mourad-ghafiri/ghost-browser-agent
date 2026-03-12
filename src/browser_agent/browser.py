"""Chrome launcher + WebSocket server bridge."""

import asyncio
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import websockets
from websockets.asyncio.server import serve


# Chrome for Testing — --load-extension was removed from branded Chrome 137+.
# Chrome for Testing (CfT) is an unbranded Chromium build where it still works.
CFT_DIR = Path.home() / ".ghost-chrome-for-testing"

CFT_URLS = {
    ("Darwin", "arm64"):  "https://storage.googleapis.com/chrome-for-testing-public/146.0.7680.72/mac-arm64/chrome-mac-arm64.zip",
    ("Darwin", "x86_64"): "https://storage.googleapis.com/chrome-for-testing-public/146.0.7680.72/mac-x64/chrome-mac-x64.zip",
    ("Linux", "x86_64"):  "https://storage.googleapis.com/chrome-for-testing-public/146.0.7680.72/linux64/chrome-linux64.zip",
    ("Windows", "AMD64"): "https://storage.googleapis.com/chrome-for-testing-public/146.0.7680.72/win64/chrome-win64.zip",
    ("Windows", "x86_64"): "https://storage.googleapis.com/chrome-for-testing-public/146.0.7680.72/win64/chrome-win64.zip",
}

CFT_BINARIES = {
    "Darwin":  "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "Linux":   "chrome-linux64/chrome",
    "Windows": "chrome-win64/chrome.exe",
}


def _get_cft_binary() -> str | None:
    """Return path to Chrome for Testing binary if installed."""
    system = platform.system()
    rel = CFT_BINARIES.get(system)
    if not rel:
        return None
    # Handle mac-x64 vs mac-arm64
    if system == "Darwin" and platform.machine() != "arm64":
        rel = rel.replace("mac-arm64", "mac-x64")
    path = CFT_DIR / rel
    if path.is_file():
        return str(path)
    return None


def _download_cft():
    """Download and extract Chrome for Testing."""
    system = platform.system()
    machine = platform.machine()
    key = (system, machine)
    url = CFT_URLS.get(key)
    if not url:
        raise RuntimeError(
            f"Chrome for Testing not available for {system}/{machine}. "
            "Install it manually from https://googlechromelabs.github.io/chrome-for-testing/"
        )

    os.makedirs(CFT_DIR, exist_ok=True)
    zip_path = CFT_DIR / "chrome.zip"

    print(f"  [browser] Downloading Chrome for Testing...")
    print(f"  [browser] URL: {url}")

    def _progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100, downloaded * 100 // total_size)
            mb = downloaded / 1024 / 1024
            total_mb = total_size / 1024 / 1024
            sys.stdout.write(f"\r  [browser] Downloading: {mb:.0f}/{total_mb:.0f} MB ({pct}%)")
            sys.stdout.flush()

    urlretrieve(url, str(zip_path), reporthook=_progress)
    print()

    print("  [browser] Extracting...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(CFT_DIR)
    zip_path.unlink()

    # Fix permissions — zipfile.extractall() doesn't preserve execute bits
    binary = _get_cft_binary()
    if not binary:
        raise RuntimeError("Chrome for Testing extracted but binary not found")

    # Make all binaries in the app bundle executable
    for dirpath, _dirnames, filenames in os.walk(str(CFT_DIR)):
        for f in filenames:
            fpath = os.path.join(dirpath, f)
            # Executables in MacOS/ dirs, Helpers, crashpad handler, .dylib
            if ("/MacOS/" in fpath or "crashpad" in f or "helper" in f.lower()
                    or f.endswith(".dylib")):
                os.chmod(fpath, 0o755)

    # Remove macOS quarantine attribute
    if system == "Darwin":
        subprocess.run(["xattr", "-c", binary], capture_output=True)
        # Also clear quarantine on the app bundle dir
        app_dir = binary
        while app_dir and not app_dir.endswith(".app"):
            app_dir = os.path.dirname(app_dir)
        if app_dir:
            for dirpath, _dirnames, filenames in os.walk(app_dir):
                for f in filenames:
                    subprocess.run(
                        ["xattr", "-c", os.path.join(dirpath, f)],
                        capture_output=True,
                    )

    print(f"  [browser] Chrome for Testing installed: {binary}")


def find_chrome() -> str:
    """Find Chrome for Testing binary, downloading it if needed."""
    binary = _get_cft_binary()
    if binary:
        return binary

    # Not installed — download it
    _download_cft()
    binary = _get_cft_binary()
    if binary:
        return binary

    raise RuntimeError("Failed to install Chrome for Testing.")


def _kill_ghost_chrome(profile_dir: str):
    """Kill any Chrome processes using our ghost profile."""
    system = platform.system()
    if system in ("Darwin", "Linux"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"--user-data-dir={profile_dir}"],
                capture_output=True, text=True,
            )
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                if pid.strip():
                    try:
                        os.kill(int(pid.strip()), signal.SIGTERM)
                    except (ProcessLookupError, ValueError):
                        pass
            if any(p.strip() for p in pids):
                import time
                time.sleep(2)
        except FileNotFoundError:
            pass
    elif system == "Windows":
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 f"commandline like '%--user-data-dir={profile_dir}%'",
                 "get", "processid"],
                capture_output=True, text=True, shell=True,
            )
            for line in result.stdout.strip().split("\n")[1:]:
                pid = line.strip()
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, shell=True)
            import time
            time.sleep(2)
        except Exception:
            pass


def _free_port(port: int):
    """Kill any process listening on the given port."""
    system = platform.system()
    try:
        if system in ("Darwin", "Linux"):
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True,
            )
            for pid in result.stdout.strip().split("\n"):
                if pid.strip().isdigit():
                    try:
                        os.kill(int(pid.strip()), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
            if result.stdout.strip():
                import time
                time.sleep(1)
        elif system == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, shell=True,
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                       capture_output=True, shell=True)
    except Exception:
        pass


def _clear_extension_cache(profile_dir: str):
    """Clear Chrome's cached extension data so --load-extension always loads fresh code.

    Only removes the minimal set of dirs needed for a fresh extension load.
    Recreates them as empty dirs so Chrome doesn't crash on missing state.
    """
    default_dir = os.path.join(profile_dir, "Default")

    # Only target the Default profile (where Chrome actually stores extension data)
    # and the service worker cache (where the compiled background.js lives)
    dirs_to_clear = []
    if os.path.isdir(default_dir):
        dirs_to_clear = [
            os.path.join(default_dir, "Extensions"),
            os.path.join(default_dir, "Local Extension Settings"),
            os.path.join(default_dir, "Service Worker"),
        ]

    cleared = False
    for d in dirs_to_clear:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)  # Recreate empty so Chrome doesn't crash
            cleared = True

    if cleared:
        print("  [browser] Cleared extension cache (fresh load)")


class BrowserBridge:
    """Async WebSocket server that communicates with the Ghost Extension."""

    def __init__(self, port: int = 7331):
        self.port = port
        self._ws = None
        self._server = None
        self._pending: dict[int, asyncio.Future] = {}
        self._msg_id = 0
        self._connected = asyncio.Event()

    async def start(self):
        # Kill anything already on our port
        _free_port(self.port)
        self._server = await serve(
            self._handler,
            "127.0.0.1",
            self.port,
        )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, websocket):
        print("  [bridge] Extension connected")
        self._ws = websocket
        self._connected.set()
        try:
            async for raw in websocket:
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    self._pending[msg_id].set_result(msg.get("result"))
        except websockets.ConnectionClosed:
            print("  [bridge] Extension disconnected")
        finally:
            self._ws = None
            self._connected.clear()

    async def send_command(self, command: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Send a command to the extension and wait for the response."""
        if not self._connected.is_set():
            print("  [bridge] Waiting for extension to connect...")
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        self._msg_id += 1
        msg_id = self._msg_id
        future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        payload = {"id": msg_id, "command": command}
        if params:
            payload["params"] = params

        await self._ws.send(json.dumps(payload))
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        finally:
            self._pending.pop(msg_id, None)

    # --- High-level API ---

    async def navigate(self, url: str) -> dict:
        return await self.send_command("navigate", {"url": url})

    async def extract_dom(self) -> dict:
        return await self.send_command("extract_dom")

    async def click(self, selector: str) -> dict:
        return await self.send_command("click", {"selector": selector})

    async def type_text(self, selector: str, text: str, clear: bool = True) -> dict:
        return await self.send_command("type", {"selector": selector, "text": text, "clear": clear})

    async def select_option(self, selector: str, value: str) -> dict:
        return await self.send_command("select", {"selector": selector, "value": value})

    async def scroll(self, direction: str = "down") -> dict:
        return await self.send_command("scroll", {"direction": direction})

    async def hover(self, selector: str) -> dict:
        return await self.send_command("hover", {"selector": selector})

    async def screenshot(self) -> dict:
        return await self.send_command("screenshot")

    async def wait(self, seconds: float = 1.0) -> dict:
        return await self.send_command("wait", {"seconds": seconds})

    async def evaluate_js(self, code: str) -> dict:
        return await self.send_command("evaluate_js", {"code": code})

    async def go_back(self) -> dict:
        return await self.send_command("back")

    async def go_forward(self) -> dict:
        return await self.send_command("forward")

    async def double_click(self, selector: str) -> dict:
        return await self.send_command("double_click", {"selector": selector})

    async def right_click(self, selector: str) -> dict:
        return await self.send_command("right_click", {"selector": selector})

    async def press_key(self, key: str, selector: str | None = None) -> dict:
        params = {"key": key}
        if selector:
            params["selector"] = selector
        return await self.send_command("press_key", params)

    async def drag_drop(self, from_selector: str, to_selector: str) -> dict:
        return await self.send_command("drag_drop", {"fromSelector": from_selector, "toSelector": to_selector})

    async def new_tab(self, url: str | None = None) -> dict:
        return await self.send_command("new_tab", {"url": url or ""})

    async def switch_tab(self, index: int) -> dict:
        return await self.send_command("switch_tab", {"index": index})

    async def close_tab(self) -> dict:
        return await self.send_command("close_tab")

    async def get_tabs(self) -> dict:
        return await self.send_command("get_tabs")

    async def get_url(self) -> dict:
        return await self.send_command("get_url")

    async def resolve_element(self, selector: str) -> dict:
        return await self.send_command("resolve", {"selector": selector})

    async def zoom(self, level: int = 100) -> dict:
        return await self.send_command("zoom", {"level": level})


class Browser:
    """Context manager: launches Chrome for Testing with the Ghost Extension."""

    def __init__(self, port: int = 7331, visible: bool = False, user_data_dir: str | None = None):
        self.port = port
        self.visible = visible
        self._user_data_dir = user_data_dir
        self._process = None
        self.bridge = BrowserBridge(port=port)

    async def __aenter__(self):
        if self._user_data_dir:
            profile_dir = self._user_data_dir
        else:
            profile_dir = str(Path.home() / ".browser-agent-profile")
        os.makedirs(profile_dir, exist_ok=True)

        extension_path = str(Path(__file__).resolve().parent.parent.parent / "extension")
        if not os.path.isfile(os.path.join(extension_path, "manifest.json")):
            raise RuntimeError(f"Extension not found at {extension_path}")

        chrome = find_chrome()

        # Kill any leftover ghost Chrome using our profile
        _kill_ghost_chrome(profile_dir)

        # Force extension refresh — clear Chrome's cached extension data
        # so the latest code from our extension/ folder is always loaded
        _clear_extension_cache(profile_dir)

        await self.bridge.start()

        args = [
            chrome,
            f"--load-extension={extension_path}",
            f"--disable-extensions-except={extension_path}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-popup-blocking",
            "--disable-translate",
            "--disable-sync",
            "--use-mock-keychain",
            "--password-store=basic",
            "--disable-infobars",
            "--disable-search-engine-choice-screen",
            "--disable-background-tracing",
            "--disable-field-trial-config",
            "--disable-features=PerfettoBackgroundTracing",
            "--window-size=1920,1080",
        ]

        if not self.visible:
            system = platform.system()
            if system in ("Darwin", "Windows"):
                args.append("--window-position=-32000,-32000")
            elif system == "Linux":
                # On Linux, prefer xvfb-run if available for true headless
                if shutil.which("xvfb-run"):
                    args = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24"] + args
                else:
                    args.append("--window-position=-32000,-32000")

        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"  [browser] Chrome for Testing launched (pid={self._process.pid})")

        # Wait for extension to connect
        try:
            await asyncio.wait_for(self.bridge._connected.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Extension did not connect within 15s. "
                "Open chrome://extensions in the ghost browser and check the extension is enabled."
            )

        return self

    async def __aexit__(self, *exc):
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            print("  [browser] Chrome stopped")

        await self.bridge.stop()
