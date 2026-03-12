"""CLI entry point for the Ghost Browser Agent."""

import argparse
import asyncio
import sys


def _load_config(args):
    """Load config.yml, with CLI args overriding config values."""
    from src.browser_agent.config import load_config
    config = load_config(getattr(args, "config", None))
    # CLI args override config values where provided
    if hasattr(args, "model") and args.model:
        config.llm.model = args.model
    if hasattr(args, "api_base") and args.api_base:
        config.llm.api_base = args.api_base
    return config


def cmd_run(args):
    """Oneshot: launch browser, run task, close browser."""
    from src.browser_agent.agent import Agent

    config = _load_config(args)

    agent = Agent(
        goal=args.task,
        url=args.url,
        max_steps=args.max_steps,
        visible=args.visible,
        model=config.llm.model,
        api_base=config.llm.api_base,
        port=args.port,
        vision_enabled=config.llm.vision_enabled,
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
    )

    print("Ghost Browser Agent [oneshot]")
    print(f"  Task: {args.task}")
    if args.url:
        print(f"  URL: {args.url}")
    print(f"  Model: {config.llm.model} @ {config.llm.api_base}")
    print(f"  Vision: {config.llm.vision_enabled} | Temp: {config.llm.temperature} | Max tokens: {config.llm.max_tokens}")
    print(f"  Max steps: {args.max_steps}")
    print()

    result = asyncio.run(agent.run())
    print(f"\n{'='*60}")
    print(f"Result: {result}")
    print(f"{'='*60}")


def cmd_start(args):
    """Start persistent browser daemon."""
    from src.browser_agent.daemon import Daemon

    config = _load_config(args)

    daemon = Daemon(
        ws_port=args.port,
        cmd_port=args.cmd_port,
        visible=True,  # persistent mode is always visible
        model=config.llm.model,
        api_base=config.llm.api_base,
        vision_enabled=config.llm.vision_enabled,
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
    )

    print("Ghost Browser Agent [persistent]")
    print(f"  Model: {config.llm.model} @ {config.llm.api_base}")
    print(f"  Vision: {config.llm.vision_enabled} | Temp: {config.llm.temperature} | Max tokens: {config.llm.max_tokens}")
    print(f"  WebSocket port: {args.port}")
    print(f"  Command port: {args.cmd_port}")
    print()

    asyncio.run(daemon.start())


def cmd_task(args):
    """Send a task to the running daemon."""
    from src.browser_agent.daemon import send_command

    print(f"Ghost Browser Agent [task]")
    print(f"  Task: {args.task}")
    if args.url:
        print(f"  URL: {args.url}")
    print(f"  Max steps: {args.max_steps}")
    print()

    async def _run():
        result = await send_command(
            "task",
            cmd_port=args.cmd_port if args.cmd_port else None,
            goal=args.task,
            max_steps=args.max_steps,
            url=args.url,
        )
        return result

    result = asyncio.run(_run())

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    elif result.get("status") == "done":
        print(f"\n{'='*60}")
        print(f"Result: {result['result']}")
        print(f"{'='*60}")
    else:
        print(f"Response: {result}")


def cmd_stop(args):
    """Stop the running daemon."""
    from src.browser_agent.daemon import send_command

    async def _run():
        return await send_command("stop", cmd_port=args.cmd_port if args.cmd_port else None)

    result = asyncio.run(_run())
    print(f"Daemon: {result.get('status', result)}")


def cmd_status(args):
    """Check daemon status."""
    from src.browser_agent.daemon import send_command

    async def _run():
        try:
            return await send_command("status", cmd_port=args.cmd_port if args.cmd_port else None)
        except Exception as e:
            return {"status": "not running", "error": str(e)}

    result = asyncio.run(_run())
    if result.get("status") == "running":
        print(f"Daemon running (pid={result.get('pid')}), extension connected={result.get('connected')}")
    else:
        print("Daemon not running")


def cmd_telegram(args):
    """Start Telegram bot mode."""
    from src.browser_agent.config import load_config
    from src.browser_agent.telegram_bot import TelegramBot

    config = load_config(getattr(args, "config", None))

    # CLI overrides
    if args.model:
        config.llm.model = args.model
    if args.api_base:
        config.llm.api_base = args.api_base
    if args.token:
        config.telegram.bot_token = args.token

    if not config.telegram.bot_token:
        print("Error: Telegram bot_token not set.")
        print("Set it in config.yml or pass --token YOUR_TOKEN")
        sys.exit(1)

    print("Ghost Browser Agent [telegram]")
    print(f"  Model: {config.llm.model} @ {config.llm.api_base}")
    print(f"  Vision: {config.llm.vision_enabled} | Temp: {config.llm.temperature} | Max tokens: {config.llm.max_tokens}")
    if config.telegram.allowed_users:
        print(f"  Allowed users: {config.telegram.allowed_users}")
    else:
        print(f"  Allowed users: all")
    print()

    bot = TelegramBot(config)
    asyncio.run(bot.start())


def main():
    parser = argparse.ArgumentParser(
        description="Ghost Browser Agent — undetectable AI browser automation",
    )
    parser.add_argument("--config", default=None, help="Path to config.yml (default: ./config.yml)")
    sub = parser.add_subparsers(dest="command")

    # --- run (oneshot) ---
    p_run = sub.add_parser("run", help="Run a single task (launch browser, do task, close)")
    p_run.add_argument("task", help="Task to accomplish")
    p_run.add_argument("--url", default=None, help="Starting URL (optional)")
    p_run.add_argument("--max-steps", type=int, default=30)
    p_run.add_argument("--visible", action="store_true", help="Show browser window")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--api-base", default=None)
    p_run.add_argument("--port", type=int, default=7331, help="WebSocket port")

    # --- start (persistent daemon) ---
    p_start = sub.add_parser("start", help="Start persistent browser (stays open)")
    p_start.add_argument("--model", default=None)
    p_start.add_argument("--api-base", default=None)
    p_start.add_argument("--port", type=int, default=7331, help="WebSocket port")
    p_start.add_argument("--cmd-port", type=int, default=7332, help="Command port")

    # --- task (send to daemon) ---
    p_task = sub.add_parser("task", help="Send a task to the running browser")
    p_task.add_argument("task", help="Task to accomplish")
    p_task.add_argument("--url", default=None, help="Navigate to URL first (optional)")
    p_task.add_argument("--max-steps", type=int, default=30)
    p_task.add_argument("--cmd-port", type=int, default=None, help="Command port")

    # --- telegram ---
    p_tg = sub.add_parser("telegram", help="Start Telegram bot mode")
    p_tg.add_argument("--token", default=None, help="Telegram bot token (overrides config.yml)")
    p_tg.add_argument("--model", default=None)
    p_tg.add_argument("--api-base", default=None)

    # --- stop ---
    p_stop = sub.add_parser("stop", help="Stop the persistent browser")
    p_stop.add_argument("--cmd-port", type=int, default=None)

    # --- status ---
    p_status = sub.add_parser("status", help="Check if daemon is running")
    p_status.add_argument("--cmd-port", type=int, default=None)

    # If first positional arg is not a known subcommand, treat it as a task (shortcut for "run")
    known_commands = {"run", "start", "task", "telegram", "stop", "status"}
    argv = sys.argv[1:]
    if argv and argv[0] not in known_commands and not argv[0].startswith("-"):
        # Insert "run" subcommand before the task string
        argv = ["run"] + argv

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        {
            "run": cmd_run,
            "start": cmd_start,
            "task": cmd_task,
            "telegram": cmd_telegram,
            "stop": cmd_stop,
            "status": cmd_status,
        }[args.command](args)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
