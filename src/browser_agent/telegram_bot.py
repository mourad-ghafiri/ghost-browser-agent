"""Telegram bot — control the Ghost Browser Agent via Telegram messages."""

import asyncio
import base64
import io
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from .agent import run_task
from .browser import Browser, BrowserBridge
from .config import Config

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot that runs browser tasks from chat messages."""

    def __init__(self, config: Config):
        self.config = config
        self._browser: Browser | None = None
        self._bridge: BrowserBridge | None = None
        self._task_lock = asyncio.Lock()
        self._cancel_event: asyncio.Event | None = None
        self._user_queue: asyncio.Queue | None = None
        self._current_chat_id: int | None = None
        self._waiting_for_user: bool = False  # True when agent is waiting for user input

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.telegram.allowed_users:
            return True
        return user_id in self.config.telegram.allowed_users

    async def start(self):
        """Start the Telegram bot + browser."""
        token = self.config.telegram.bot_token
        if not token:
            raise RuntimeError(
                "Telegram bot_token not set. Add it to config.yml under telegram.bot_token"
            )

        # Launch browser
        self._browser = Browser(
            port=self.config.browser.ws_port,
            visible=True,
        )
        await self._browser.__aenter__()
        self._bridge = self._browser.bridge
        print("  [telegram] Browser launched")

        # Build Telegram app
        app = Application.builder().token(token).concurrent_updates(True).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("screenshot", self._cmd_screenshot))
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("reject", self._cmd_reject))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

        # Store app ref for sending messages from callbacks
        self._app = app

        print("  [telegram] Bot started — send a message to control the browser")
        print("  [telegram] Press Ctrl+C to stop\n")

        # Run bot
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            # Register commands in Telegram menu
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("start", "Start the bot"),
                BotCommand("help", "Show available commands"),
                BotCommand("screenshot", "Get current page screenshot"),
                BotCommand("stop", "Cancel the running task"),
                BotCommand("reject", "Reject agent's question and pause task"),
            ])

            # Keep alive
            stop_event = asyncio.Event()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            await self._cleanup()

    async def _cleanup(self):
        if self._browser:
            await self._browser.__aexit__(None, None, None)
            self._browser = None
            self._bridge = None
            print("  [telegram] Browser stopped")

    async def _wait_for_lock(self):
        """Poll until the task lock is released."""
        while self._task_lock.locked():
            await asyncio.sleep(0.1)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return
        await update.message.reply_text(
            "Ghost Browser Agent ready!\n\n"
            "Send me any task and I'll do it in the browser.\n\n"
            "The agent will ask for your confirmation before sensitive actions "
            "(passwords, payments, personal info).\n\n"
            "Commands:\n"
            "/stop — Cancel the running task\n"
            "/reject — Decline agent's question and pause\n"
            "/screenshot — Get current page screenshot\n"
            "/help — Show this message"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "Send any text message as a browser task.\n\n"
            "Examples:\n"
            '- "Search Google for weather in Paris"\n'
            '- "Go to amazon.com and find the cheapest laptop"\n'
            '- "Log into my email"\n\n'
            "When the agent asks a question:\n"
            "- Reply with your answer to continue\n"
            "- /reject — Decline and pause the task\n\n"
            "Other commands:\n"
            "- /stop — Cancel the running task\n"
            "- /screenshot — Get current page screenshot"
        )

    async def _cmd_screenshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._bridge:
            await update.message.reply_text("Browser not running.")
            return

        try:
            ss = await asyncio.wait_for(self._bridge.screenshot(), timeout=5.0)
            if "error" in ss:
                await update.message.reply_text(f"Screenshot error: {ss['error']}")
                return
            img_bytes = base64.b64decode(ss["image"])
            await update.message.reply_photo(
                photo=io.BytesIO(img_bytes),
                caption="Current page"
            )
        except Exception as e:
            await update.message.reply_text(f"Screenshot failed: {e}")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return

        # If waiting for user input, unblock with None (rejection) + cancel
        if self._waiting_for_user and self._user_queue:
            await self._user_queue.put(None)
            self._waiting_for_user = False

        if self._cancel_event and self._task_lock.locked():
            self._cancel_event.set()
            await update.message.reply_text(
                "Stopping current task... Browser stays open.\n"
                "Send a new message to continue from the current page."
            )
        else:
            await update.message.reply_text("No task is running.")

    async def _cmd_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reject the agent's question and pause the task."""
        if not self._is_allowed(update.effective_user.id):
            return

        if self._waiting_for_user and self._user_queue:
            await self._user_queue.put(None)  # None = rejected
            self._waiting_for_user = False
            await update.message.reply_text(
                "Action declined. Task paused.\n"
                "Browser stays on current page — send a new message to continue."
            )
        else:
            await update.message.reply_text("No pending question to reject.")

    async def _send_step_update(self, step: int, max_steps: int, action_text: str, screenshot_b64: str):
        """Called by the agent loop at each step to send live updates to Telegram."""
        if not self._current_chat_id:
            return

        bot = self._app.bot

        # Send screenshot with action as caption
        if screenshot_b64:
            try:
                img_bytes = base64.b64decode(screenshot_b64)
                caption = action_text[:1024]  # Telegram caption limit
                await bot.send_photo(
                    chat_id=self._current_chat_id,
                    photo=io.BytesIO(img_bytes),
                    caption=caption,
                )
                return
            except Exception:
                pass

        # Fallback: text only
        try:
            await bot.send_message(
                chat_id=self._current_chat_id,
                text=action_text,
            )
        except Exception:
            pass

    async def _ask_user(self, question: str) -> str | None:
        """Send question to Telegram user and wait for their reply.

        Returns the user's text response, or None if they rejected/timed out.
        """
        if not self._current_chat_id or not self._user_queue:
            return None

        self._waiting_for_user = True
        bot = self._app.bot

        try:
            await bot.send_message(
                chat_id=self._current_chat_id,
                text=(
                    f"Agent needs your input:\n\n"
                    f"{question}\n\n"
                    f"Reply with your answer, or /reject to decline."
                ),
            )
        except Exception:
            self._waiting_for_user = False
            return None

        # Wait for user response (5 min timeout)
        try:
            response = await asyncio.wait_for(self._user_queue.get(), timeout=300)
            return response
        except asyncio.TimeoutError:
            try:
                await bot.send_message(
                    chat_id=self._current_chat_id,
                    text="No response received (5 min timeout). Task paused.",
                )
            except Exception:
                pass
            return None
        finally:
            self._waiting_for_user = False

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages."""
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        if not self._bridge:
            await update.message.reply_text("Browser not running.")
            return

        text = update.message.text.strip()
        if not text:
            return

        # If agent is waiting for user input, send response to the queue
        if self._waiting_for_user and self._user_queue:
            await self._user_queue.put(text)
            await update.message.reply_text("Got it, continuing...")
            return

        # If a task is running, cancel it first — new message becomes the new goal
        if self._task_lock.locked() and self._cancel_event:
            self._cancel_event.set()
            await update.message.reply_text(f"Stopping current task...\nNew task: {text}")
            # Wait for the lock to be released (current task stops)
            try:
                await asyncio.wait_for(self._wait_for_lock(), timeout=10.0)
            except asyncio.TimeoutError:
                await update.message.reply_text("Previous task didn't stop in time. Try again.")
                return
        else:
            await update.message.reply_text(f"Running: {text}")

        self._cancel_event = asyncio.Event()
        self._user_queue = asyncio.Queue()
        self._current_chat_id = update.effective_chat.id

        async with self._task_lock:
            try:
                result = await run_task(
                    bridge=self._bridge,
                    goal=text,
                    model=self.config.llm.model,
                    api_base=self.config.llm.api_base,
                    max_steps=self.config.agent.max_steps,
                    vision_enabled=self.config.llm.vision_enabled,
                    temperature=self.config.llm.temperature,
                    max_tokens=self.config.llm.max_tokens,
                    on_step=self._send_step_update,
                    on_ask_user=self._ask_user,
                    cancel_event=self._cancel_event,
                    user_queue=self._user_queue,
                )

                # Send final result
                await update.message.reply_text(f"Result: {result}")

            except Exception as e:
                await update.message.reply_text(f"Task failed: {e}")
            finally:
                self._cancel_event = None
                self._user_queue = None
                self._current_chat_id = None
                self._waiting_for_user = False
