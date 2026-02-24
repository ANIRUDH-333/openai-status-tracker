from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from status_tracker.events import ConsoleHandler, EventBus, run_consumer
from status_tracker.models import EventType, Incident, PageConfig, StatusEvent
from status_tracker.monitor import FeedMonitor
from status_tracker.web import WebHandler, create_web_app

# Number of recent incidents to show on dashboard at startup
SEED_LIMIT = 5

logger = logging.getLogger(__name__)

# Default pages to monitor
DEFAULT_PAGES: list[PageConfig] = [
    PageConfig(name="OpenAI", base_url="https://status.openai.com"),
]

# Max concurrent HTTP connections across all monitors
MAX_CONCURRENT_CONNECTIONS = 20


def load_config(path: str) -> list[PageConfig]:
    """Load page configs from a JSON file.

    Expected format:
    {
      "pages": [
        {"name": "OpenAI", "base_url": "https://status.openai.com"},
        {"name": "GitHub", "base_url": "https://www.githubstatus.com", "poll_interval": 90}
      ]
    }
    """
    data = json.loads(Path(path).read_text())
    pages = []
    for entry in data["pages"]:
        pages.append(PageConfig(**entry))
    return pages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="status_tracker",
        description="Monitor status pages for incidents via Atom feeds.",
    )
    parser.add_argument(
        "--config",
        help="Path to JSON config file with page definitions",
    )
    parser.add_argument(
        "--pages",
        nargs="+",
        metavar="NAME=URL",
        help="Pages to monitor as NAME=URL pairs (e.g. OpenAI=https://status.openai.com)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=60.0,
        help="Base poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-poll-interval",
        type=float,
        default=300.0,
        help="Maximum poll interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=MAX_CONCURRENT_CONNECTIONS,
        help=f"Max concurrent HTTP connections (default: {MAX_CONCURRENT_CONNECTIONS})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for web dashboard (default: PORT env var or 8080)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable web dashboard, console only",
    )
    return parser.parse_args(argv)


def build_pages(args: argparse.Namespace) -> list[PageConfig]:
    """Build page list from CLI args, config file, or defaults."""
    if args.config:
        return load_config(args.config)

    if args.pages:
        pages = []
        for pair in args.pages:
            if "=" not in pair:
                logger.error("Invalid page format: %s (expected NAME=URL)", pair)
                sys.exit(1)
            name, url = pair.split("=", 1)
            pages.append(PageConfig(
                name=name,
                base_url=url,
                poll_interval=args.poll_interval,
                max_poll_interval=args.max_poll_interval,
            ))
        return pages

    return [
        PageConfig(
            name=p.name,
            base_url=p.base_url,
            feed_path=p.feed_path,
            poll_interval=args.poll_interval,
            max_poll_interval=args.max_poll_interval,
            backoff_factor=p.backoff_factor,
        )
        for p in DEFAULT_PAGES
    ]


async def run(
    pages: list[PageConfig] | None = None,
    concurrency: int = MAX_CONCURRENT_CONNECTIONS,
    web_port: int | None = None,
    no_web: bool = False,
) -> None:
    """Start monitoring all configured status pages."""
    if not pages:
        logger.warning("No pages configured, exiting.")
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    event_bus = EventBus()
    console_handler = ConsoleHandler()
    web_handler = WebHandler()
    web_handler.set_page_count(len(pages))
    semaphore = asyncio.Semaphore(concurrency)

    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # Start web server if enabled
    web_runner = None
    if not no_web:
        port = web_port or int(os.environ.get("PORT", 8080))
        app = create_web_app(web_handler)
        web_runner = web.AppRunner(app)
        await web_runner.setup()
        site = web.TCPSite(web_runner, "0.0.0.0", port)
        await site.start()
        logger.info("Web dashboard running at http://0.0.0.0:%d", port)

    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=5,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )

    def _seed_dashboard(page_name: str, incidents: list[Incident]) -> None:
        """Push recent historical incidents to the web dashboard on first poll."""
        if no_web:
            return
        # Send oldest first so newest ends up at top (appendleft in WebHandler)
        for inc in reversed(incidents[:SEED_LIMIT]):
            status_lower = inc.status_text.lower()
            if status_lower in ("resolved", "postmortem"):
                event_type = EventType.INCIDENT_RESOLVED
            else:
                event_type = EventType.NEW_INCIDENT
            web_handler.handle(StatusEvent(
                event_type=event_type,
                page_name=page_name,
                incident=inc,
            ))

    async with aiohttp.ClientSession(connector=connector) as session:
        monitors = [
            FeedMonitor(page, event_bus, session, semaphore, on_seed=_seed_dashboard)
            for page in pages
        ]

        # Start all monitor tasks + consumer
        monitor_tasks = [asyncio.create_task(m.run()) for m in monitors]
        consumer_task = asyncio.create_task(
            run_consumer(event_bus, console_handler, web_handler)
        )

        print(f"Monitoring {len(pages)} status page(s): {', '.join(p.name for p in pages)}")
        print(f"Poll interval: {pages[0].poll_interval}s (max {pages[0].max_poll_interval}s)")
        print(f"Concurrency: {concurrency} connections")
        if not no_web:
            print(f"Web dashboard: http://localhost:{port}")
        print("Press Ctrl+C to stop.\n")

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Cancel monitor tasks
        for task in monitor_tasks:
            task.cancel()
        await asyncio.gather(*monitor_tasks, return_exceptions=True)

        # Log metrics on shutdown
        for m in monitors:
            logger.info(
                "%s stats: polls=%d, 304s=%d, failures=%d, events=%d",
                m._config.name, m.polls_total, m.polls_304, m.polls_failed, m.events_emitted,
            )

        # Signal consumer to stop and wait
        await event_bus.shutdown()
        await consumer_task

    if web_runner:
        await web_runner.cleanup()

    logger.info("Shutdown complete")


def main() -> None:
    args = parse_args()
    pages = build_pages(args)
    try:
        asyncio.run(run(
            pages=pages,
            concurrency=args.concurrency,
            web_port=args.port,
            no_web=args.no_web,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
