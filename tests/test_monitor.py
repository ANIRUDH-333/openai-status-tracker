from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone

import aiohttp
import pytest
from aiohttp import web

from status_tracker.events import EventBus
from status_tracker.models import EventType, PageConfig, StatusEvent
from status_tracker.monitor import FeedMonitor


def _make_feed(entries_xml: str, feed_updated: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <updated>{feed_updated}</updated>
  {entries_xml}
</feed>"""


def _make_entry(entry_id: str, title: str, updated: str, status: str, components: list[tuple[str, str]] | None = None) -> str:
    comp_html = ""
    if components:
        items = "".join(f"<li>{name} ({st})</li>" for name, st in components)
        comp_html = f"<ul>{items}</ul>"
    content = html.escape(f"<b>Status: {status}</b>{comp_html}")
    return f"""\
<entry>
  <id>{entry_id}</id>
  <title>{title}</title>
  <updated>{updated}</updated>
  <link href="https://example.com/incidents/{entry_id}"/>
  <content type="html">{content}</content>
</entry>"""


class MockFeedServer:
    """Test helper: serves configurable Atom feed responses."""

    def __init__(self):
        self.feed_content: str = _make_feed("", "2025-01-01T00:00:00+00:00")
        self.status_code: int = 200
        self.request_count: int = 0
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    async def _handle(self, request: web.Request) -> web.Response:
        self.request_count += 1
        if self.status_code != 200:
            return web.Response(status=self.status_code)
        return web.Response(text=self.feed_content, content_type="application/atom+xml")

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/feed.atom", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        # Get the dynamically assigned port
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


async def _collect_events(bus: EventBus, count: int, timeout: float = 5.0) -> list[StatusEvent]:
    """Collect exactly `count` events from the bus, with timeout."""
    events = []
    try:
        for _ in range(count):
            event = await asyncio.wait_for(bus.consume(), timeout=timeout)
            if event is not None:
                events.append(event)
    except asyncio.TimeoutError:
        pass
    return events


@pytest.fixture
async def server():
    s = MockFeedServer()
    await s.start()
    yield s
    await s.stop()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def config(server: MockFeedServer):
    return PageConfig(
        name="Test",
        base_url=f"http://127.0.0.1:{server.port}",
        poll_interval=0.5,
        max_poll_interval=2.0,
    )


@pytest.mark.asyncio
async def test_first_poll_seeds_silently(server, bus, config):
    """First poll should populate _known without emitting events."""
    entry = _make_entry("inc-1", "Old incident", "2025-01-01T00:00:00+00:00", "Resolved")
    server.feed_content = _make_feed(entry, "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        # Wait for first poll to complete
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # No events should have been emitted
    events = await _collect_events(bus, 1, timeout=0.5)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_new_incident_detected(server, bus, config):
    """A new incident appearing after seeding should emit NEW_INCIDENT."""
    # Seed with empty feed
    server.feed_content = _make_feed("", "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        # Wait for seed poll
        await asyncio.sleep(1)

        # Now add a new incident
        entry = _make_entry("inc-new", "API is down", "2025-06-01T00:00:00+00:00", "Investigating",
                            [("API", "Major Outage")])
        server.feed_content = _make_feed(entry, "2025-06-01T00:00:00+00:00")

        events = await _collect_events(bus, 1, timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1
    assert events[0].event_type == EventType.NEW_INCIDENT
    assert events[0].incident.title == "API is down"
    assert events[0].incident.status_text == "Investigating"
    assert events[0].incident.affected_components[0].name == "API"


@pytest.mark.asyncio
async def test_incident_update_detected(server, bus, config):
    """Status change on a known incident should emit INCIDENT_UPDATED."""
    entry = _make_entry("inc-1", "Latency issue", "2025-01-01T00:00:00+00:00", "Investigating")
    server.feed_content = _make_feed(entry, "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        await asyncio.sleep(1)

        # Update status
        entry = _make_entry("inc-1", "Latency issue", "2025-01-01T01:00:00+00:00", "Identified")
        server.feed_content = _make_feed(entry, "2025-01-01T01:00:00+00:00")

        events = await _collect_events(bus, 1, timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1
    assert events[0].event_type == EventType.INCIDENT_UPDATED
    assert events[0].incident.status_text == "Identified"
    assert events[0].previous_status == "Investigating"


@pytest.mark.asyncio
async def test_incident_resolved_detected(server, bus, config):
    """Resolved status should emit INCIDENT_RESOLVED."""
    entry = _make_entry("inc-1", "Outage", "2025-01-01T00:00:00+00:00", "Investigating")
    server.feed_content = _make_feed(entry, "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        await asyncio.sleep(1)

        entry = _make_entry("inc-1", "Outage", "2025-01-01T02:00:00+00:00", "Resolved",
                            [("API", "Operational")])
        server.feed_content = _make_feed(entry, "2025-01-01T02:00:00+00:00")

        events = await _collect_events(bus, 1, timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1
    assert events[0].event_type == EventType.INCIDENT_RESOLVED
    assert events[0].previous_status == "Investigating"


@pytest.mark.asyncio
async def test_unchanged_feed_emits_nothing(server, bus, config):
    """Polling an unchanged feed should not emit any events."""
    entry = _make_entry("inc-1", "Old", "2025-01-01T00:00:00+00:00", "Resolved")
    server.feed_content = _make_feed(entry, "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        # Wait for multiple poll cycles
        await asyncio.sleep(3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    events = await _collect_events(bus, 1, timeout=0.5)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_server_error_recovery(server, bus, config):
    """Monitor should survive server errors and recover."""
    entry = _make_entry("inc-1", "Test", "2025-01-01T00:00:00+00:00", "Resolved")
    server.feed_content = _make_feed(entry, "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        await asyncio.sleep(1)

        # Server goes down
        server.status_code = 500
        await asyncio.sleep(2)

        # Server recovers with a new incident
        server.status_code = 200
        entry_new = _make_entry("inc-2", "New after error", "2025-06-01T00:00:00+00:00", "Investigating")
        server.feed_content = _make_feed(
            entry_new + "\n" + entry,
            "2025-06-01T00:00:00+00:00",
        )

        events = await _collect_events(bus, 1, timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1
    assert events[0].event_type == EventType.NEW_INCIDENT
    assert events[0].incident.title == "New after error"


@pytest.mark.asyncio
async def test_metrics_tracked(server, bus, config):
    """Monitor should track poll/failure/event metrics."""
    server.feed_content = _make_feed("", "2025-01-01T00:00:00+00:00")

    async with aiohttp.ClientSession() as session:
        monitor = FeedMonitor(config, bus, session, asyncio.Semaphore(5))
        task = asyncio.create_task(monitor.run())

        await asyncio.sleep(2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert monitor.polls_total >= 2
    assert monitor.polls_failed == 0
