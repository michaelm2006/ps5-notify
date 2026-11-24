from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from ps5_notify import __version__
from ps5_notify.config import ConfigError, Settings, load_settings
from ps5_notify.discord import DiscordWebhookClient, DiscordWebhookError
from ps5_notify.fetcher import PageFetcher, create_fetcher
from ps5_notify.monitor import StockMonitor
from ps5_notify.state import StateStore

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps5-notify",
        description="Monitor UK retailers for PS5 Pro restocks and notify via Discord webhook.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to a .env file (defaults to ./.env if present).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check cycle across all enabled retailers and exit.",
    )
    parser.add_argument(
        "--test-webhook",
        action="store_true",
        help="Send a test embed to the configured Discord webhook and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check stock and log results without sending Discord webhooks.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=None,
        help="Override LOG_LEVEL from .env.",
    )
    return parser


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    if level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _run_test_webhook(settings: Settings) -> int:
    client = DiscordWebhookClient(
        settings.discord_webhook_url,
        mention=settings.discord_mention,
        timeout_seconds=settings.http_timeout_seconds,
    )
    try:
        await client.send_test_alert()
    except DiscordWebhookError as exc:
        logger.error("Webhook test failed: %s", exc)
        return 1
    finally:
        await client.close()

    logger.info("Test webhook delivered successfully.")
    return 0


async def _run_monitor(settings: Settings, *, once: bool, dry_run: bool) -> int:
    backends = sorted({target.fetcher_backend for target in settings.targets})
    fetchers: dict[str, PageFetcher] = {
        backend: create_fetcher(backend, timeout_seconds=settings.http_timeout_seconds)
        for backend in backends
    }

    notifier: DiscordWebhookClient | None = None
    if not dry_run:
        notifier = DiscordWebhookClient(
            settings.discord_webhook_url,
            mention=settings.discord_mention,
            timeout_seconds=settings.http_timeout_seconds,
        )

    state_store = StateStore(settings.state_file_path)
    monitor = StockMonitor(
        settings=settings,
        state_store=state_store,
        fetchers=fetchers,
        notifier=notifier,
    )

    try:
        if once:
            outcomes = await monitor.run_once()
            all_failed = bool(outcomes) and all(outcome.error is not None for outcome in outcomes)
            return 1 if all_failed else 0

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_shutdown(sig_name: str) -> None:
            logger.info("Received %s; shutting down gracefully...", sig_name)
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown, sig.name)
            except NotImplementedError:
                # Windows event loops do not support loop.add_signal_handler.
                signal.signal(sig, lambda s, _f: loop.call_soon_threadsafe(_request_shutdown, signal.Signals(s).name))

        await monitor.run_forever(stop_event)
        return 0
    finally:
        for fetcher in fetchers.values():
            await fetcher.close()
        if notifier is not None:
            await notifier.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.test_webhook and args.dry_run:
        print("Error: --test-webhook cannot be combined with --dry-run.", file=sys.stderr)
        return 2

    require_webhook = not args.dry_run
    try:
        settings = load_settings(args.env_file, require_webhook=require_webhook)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or settings.log_level)

    try:
        if args.test_webhook:
            return asyncio.run(_run_test_webhook(settings))
        return asyncio.run(_run_monitor(settings, once=args.once, dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
