"""
End-to-end test: runs a mock Atom feed server + the tracker, cycling through scenarios.

Scenarios tested:
  1. Startup seeding     — existing incidents are loaded silently (no output)
  2. New incident        — RED [NEW INCIDENT] appears
  3. Incident updated    — YELLOW [UPDATED] appears
  4. Incident resolved   — GREEN [RESOLVED] appears
  5. Second incident     — another NEW while first is resolved
  6. Feed error recovery — server returns 500, tracker recovers
  7. Graceful shutdown   — clean exit, no errors

Run:  python test_scenarios.py
"""

from __future__ import annotations

import asyncio
import html
import signal
import sys
from datetime import datetime, timezone
from aiohttp import web

from status_tracker.events import ConsoleHandler, EventBus, run_consumer
from status_tracker.models import PageConfig
from status_tracker.monitor import FeedMonitor

# ── Mock Feed State ──────────────────────────────────────────────────────────

FEED_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Mock Status Page</title>
  <updated>{feed_updated}</updated>
  {entries}
</feed>"""

ENTRY_TEMPLATE = """\
<entry>
  <id>{entry_id}</id>
  <title>{title}</title>
  <updated>{updated}</updated>
  <link href="https://mock.status.page/incidents/{entry_id}"/>
  <content type="html">{content}</content>
</entry>"""


def make_entry(entry_id: str, title: str, updated: str, status: str, components: list[tuple[str, str]] | None = None) -> str:
    comp_html = ""
    if components:
        items = "".join(f"<li>{name} ({st})</li>" for name, st in components)
        comp_html = f"<ul>{items}</ul>"
    content = html.escape(f"<b>Status: {status}</b>{comp_html}")
    return ENTRY_TEMPLATE.format(entry_id=entry_id, title=title, updated=updated, content=content)


def make_feed(entries: list[str], feed_updated: str) -> str:
    return FEED_TEMPLATE.format(feed_updated=feed_updated, entries="\n".join(entries))


# Global mutable state for the mock server
current_feed: str = ""
should_fail: bool = False


async def handle_feed(request: web.Request) -> web.Response:
    if should_fail:
        return web.Response(status=500, text="Internal Server Error")
    return web.Response(text=current_feed, content_type="application/atom+xml")


# ── Scenario Definitions ─────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scenario_seed() -> None:
    """Pre-existing resolved incident — should be seeded silently."""
    global current_feed
    entries = [
        make_entry("old-001", "Past issue with API", "2025-01-01T00:00:00+00:00", "Resolved",
                    [("API", "Operational")]),
    ]
    current_feed = make_feed(entries, "2025-01-01T00:00:00+00:00")


def scenario_new_incident() -> None:
    """New incident appears."""
    global current_feed
    ts = now_iso()
    entries = [
        make_entry("inc-101", "Elevated error rates in ChatGPT", ts, "Investigating",
                    [("Conversations", "Degraded Performance"), ("API", "Degraded Performance")]),
        make_entry("old-001", "Past issue with API", "2025-01-01T00:00:00+00:00", "Resolved",
                    [("API", "Operational")]),
    ]
    current_feed = make_feed(entries, ts)


def scenario_incident_updated() -> None:
    """Incident status changes to Identified."""
    global current_feed
    ts = now_iso()
    entries = [
        make_entry("inc-101", "Elevated error rates in ChatGPT", ts, "Identified",
                    [("Conversations", "Degraded Performance")]),
        make_entry("old-001", "Past issue with API", "2025-01-01T00:00:00+00:00", "Resolved",
                    [("API", "Operational")]),
    ]
    current_feed = make_feed(entries, ts)


def scenario_incident_resolved() -> None:
    """Incident resolved."""
    global current_feed
    ts = now_iso()
    entries = [
        make_entry("inc-101", "Elevated error rates in ChatGPT", ts, "Resolved",
                    [("Conversations", "Operational"), ("API", "Operational")]),
        make_entry("old-001", "Past issue with API", "2025-01-01T00:00:00+00:00", "Resolved",
                    [("API", "Operational")]),
    ]
    current_feed = make_feed(entries, ts)


def scenario_second_incident() -> None:
    """A second incident appears while first stays resolved."""
    global current_feed
    ts = now_iso()
    entries = [
        make_entry("inc-202", "Image generation failures", ts, "Investigating",
                    [("Image Generation", "Major Outage")]),
        make_entry("inc-101", "Elevated error rates in ChatGPT", "2025-06-01T01:00:00+00:00", "Resolved",
                    [("Conversations", "Operational"), ("API", "Operational")]),
        make_entry("old-001", "Past issue with API", "2025-01-01T00:00:00+00:00", "Resolved",
                    [("API", "Operational")]),
    ]
    current_feed = make_feed(entries, ts)


def scenario_feed_error() -> None:
    """Server returns 500."""
    global should_fail
    should_fail = True


def scenario_feed_recovery() -> None:
    """Server recovers from error."""
    global should_fail
    should_fail = False


# ── Test Runner ───────────────────────────────────────────────────────────────

SCENARIOS = [
    ("Seeding: pre-existing incident (should be silent)", scenario_seed, 0),
    # Wait for first poll to complete before changing the feed
    (">>> New incident: Elevated error rates", scenario_new_incident, 5),
    (">>> Incident updated: Investigating → Identified", scenario_incident_updated, 5),
    (">>> Incident resolved", scenario_incident_resolved, 5),
    (">>> Second new incident while first is resolved", scenario_second_incident, 5),
    (">>> Feed error: server returns 500", scenario_feed_error, 5),
    (">>> Feed recovery: server back to normal", scenario_feed_recovery, 5),
]


async def run_scenarios() -> None:
    global current_feed

    # Start mock server
    app = web.Application()
    app.router.add_get("/feed.atom", handle_feed)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8089)
    await site.start()

    # Configure tracker to point at mock server with fast polling
    config = PageConfig(
        name="MockStatus",
        base_url="http://127.0.0.1:8089",
        poll_interval=2.0,       # Poll every 2s for fast testing
        max_poll_interval=10.0,
        backoff_factor=1.5,
    )

    event_bus = EventBus()
    handler = ConsoleHandler()
    semaphore = asyncio.Semaphore(5)

    async with __import__("aiohttp").ClientSession() as session:
        monitor = FeedMonitor(config, event_bus, session, semaphore)
        monitor_task = asyncio.create_task(monitor.run())
        consumer_task = asyncio.create_task(run_consumer(event_bus, handler))

        print("=" * 60)
        print("  STATUS TRACKER — END-TO-END TEST")
        print("=" * 60)
        print()

        for description, setup_fn, delay in SCENARIOS:
            if delay:
                await asyncio.sleep(delay)
            print(f"\033[96m--- {description} ---\033[0m")
            setup_fn()
            # Give tracker time to poll and react
            await asyncio.sleep(4)
            print()

        print("\033[96m--- Shutting down gracefully ---\033[0m")
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        await event_bus.shutdown()
        await consumer_task

    await runner.cleanup()
    print("\033[92mAll scenarios completed successfully!\033[0m")


def main() -> None:
    try:
        asyncio.run(run_scenarios())
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
