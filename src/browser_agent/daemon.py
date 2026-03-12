"""Persistent browser daemon — keeps Chrome open, accepts tasks via TCP."""

import asyncio
import json
import os
import signal
from pathlib import Path

from .browser import Browser
from .agent import run_task


DAEMON_DIR = Path.home() / ".ghost-daemon"
PID_FILE = DAEMON_DIR / "pid"
PORT_FILE = DAEMON_DIR / "port"
DEFAULT_CMD_PORT = 7332


class Daemon:
    """Persistent browser that stays open and accepts tasks."""

    def __init__(
        self,
        ws_port: int = 7331,
        cmd_port: int = DEFAULT_CMD_PORT,
        visible: bool = True,
        model: str = "qwen3.5-27b",
        api_base: str = "http://localhost:1234/v1",
        vision_enabled: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ):
        self.ws_port = ws_port
        self.cmd_port = cmd_port
        self.visible = visible
        self.model = model
        self.api_base = api_base
        self.vision_enabled = vision_enabled
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._browser = None
        self._cmd_server = None
        self._running = False
        self._task_lock = asyncio.Lock()

    async def start(self):
        """Start the daemon: launch browser + command server."""
        os.makedirs(DAEMON_DIR, exist_ok=True)

        # Check if already running
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(old_pid, 0)
                raise RuntimeError(f"Daemon already running (pid={old_pid}). Run 'stop' first.")
            except ProcessLookupError:
                pass  # Stale PID file

        self._running = True

        # Launch browser (always visible in persistent mode for natural browsing)
        self._browser = Browser(port=self.ws_port, visible=self.visible)
        await self._browser.__aenter__()

        # Start command server
        self._cmd_server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.cmd_port,
        )
        print(f"  [daemon] Command server on 127.0.0.1:{self.cmd_port}")

        # Write PID + port files
        PID_FILE.write_text(str(os.getpid()))
        PORT_FILE.write_text(str(self.cmd_port))

        print(f"  [daemon] Ready — browser is open, send tasks with: uv run cli.py task \"your task\"")
        print(f"  [daemon] Press Ctrl+C to stop\n")

        # Handle signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.stop()))

        # Keep alive
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def stop(self):
        self._running = False

    async def _cleanup(self):
        if self._cmd_server:
            self._cmd_server.close()
        if self._browser:
            await self._browser.__aexit__(None, None, None)
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)
        print("  [daemon] Stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a CLI client connection."""
        try:
            raw = await reader.readline()
            if not raw:
                return
            msg = json.loads(raw.decode())
            cmd = msg.get("cmd")

            if cmd == "stop":
                writer.write(json.dumps({"status": "stopping"}).encode() + b"\n")
                await writer.drain()
                await self.stop()

            elif cmd == "status":
                connected = self._browser.bridge._connected.is_set() if self._browser else False
                writer.write(json.dumps({
                    "status": "running",
                    "connected": connected,
                    "pid": os.getpid(),
                }).encode() + b"\n")
                await writer.drain()

            elif cmd == "task":
                goal = msg.get("goal", "")
                max_steps = msg.get("max_steps", 30)
                url = msg.get("url")

                if not goal:
                    writer.write(json.dumps({"error": "no goal provided"}).encode() + b"\n")
                    await writer.drain()
                    return

                # Only one task at a time
                if self._task_lock.locked():
                    writer.write(json.dumps({"error": "a task is already running"}).encode() + b"\n")
                    await writer.drain()
                    return

                writer.write(json.dumps({"status": "running", "goal": goal}).encode() + b"\n")
                await writer.drain()

                async with self._task_lock:
                    result = await run_task(
                        bridge=self._browser.bridge,
                        goal=goal,
                        model=self.model,
                        api_base=self.api_base,
                        max_steps=max_steps,
                        start_url=url,
                        vision_enabled=self.vision_enabled,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                    )

                writer.write(json.dumps({"status": "done", "result": result}).encode() + b"\n")
                await writer.drain()

            else:
                writer.write(json.dumps({"error": f"unknown command: {cmd}"}).encode() + b"\n")
                await writer.drain()

        except Exception as e:
            try:
                writer.write(json.dumps({"error": str(e)}).encode() + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()


async def send_command(cmd: str, cmd_port: int | None = None, **kwargs) -> dict:
    """Send a command to the running daemon and return the response."""
    if cmd_port is None:
        if PORT_FILE.exists():
            cmd_port = int(PORT_FILE.read_text().strip())
        else:
            raise RuntimeError("No daemon running. Start one with: uv run cli.py start")

    reader, writer = await asyncio.open_connection("127.0.0.1", cmd_port)
    msg = {"cmd": cmd, **kwargs}
    writer.write(json.dumps(msg).encode() + b"\n")
    await writer.drain()

    lines = []
    while True:
        line = await reader.readline()
        if not line:
            break
        lines.append(json.loads(line.decode()))

    writer.close()
    return lines[-1] if lines else {"error": "no response"}
