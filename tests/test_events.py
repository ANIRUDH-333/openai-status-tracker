from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO

import pytest

from status_tracker.events import ConsoleHandler, EventBus, run_consumer
from status_tracker.models import (
    AffectedComponent,
    EventType,
    Incident,
    StatusEvent,
)


def _make_event(
    event_type: EventType = EventType.NEW_INCIDENT,
    title: str = "Test incident",
    status: str = "Investigating",
    previous: str | None = None,
) -> StatusEvent:
    return StatusEvent(
        event_type=event_type,
        page_name="TestPage",
        incident=Incident(
            id="inc-1",
            title=title,
            updated=datetime(2025, 6, 1, tzinfo=timezone.utc),
            status_text=status,
            message="",
            affected_components=(AffectedComponent("API", "Degraded"),),
            link="https://example.com/inc-1",
        ),
        previous_status=previous,
    )


class TestEventBus:
    @pytest.mark.asyncio
    async def test_emit_and_consume(self):
        bus = EventBus()
        event = _make_event()
        await bus.emit(event)
        received = await bus.consume()
        assert received is event

    @pytest.mark.asyncio
    async def test_shutdown_returns_none(self):
        bus = EventBus()
        await bus.shutdown()
        result = await bus.consume()
        assert result is None

    @pytest.mark.asyncio
    async def test_emit_drops_on_full_queue(self):
        bus = EventBus(maxsize=2)
        await bus.emit(_make_event())
        await bus.emit(_make_event())
        # Third should be dropped, not block
        await bus.emit(_make_event())
        # Only 2 in queue
        e1 = await bus.consume()
        e2 = await bus.consume()
        assert e1 is not None
        assert e2 is not None

    @pytest.mark.asyncio
    async def test_ordering_preserved(self):
        bus = EventBus()
        events = [
            _make_event(title="First"),
            _make_event(title="Second"),
            _make_event(title="Third"),
        ]
        for e in events:
            await bus.emit(e)
        for expected in events:
            received = await bus.consume()
            assert received is not None
            assert received.incident.title == expected.incident.title


class TestConsoleHandler:
    def test_prints_new_incident(self, capsys):
        handler = ConsoleHandler()
        handler.handle(_make_event(EventType.NEW_INCIDENT))
        output = capsys.readouterr().out
        assert "NEW INCIDENT" in output
        assert "TestPage" in output
        assert "Test incident" in output
        assert "Investigating" in output
        assert "API (Degraded)" in output

    def test_prints_updated_with_previous(self, capsys):
        handler = ConsoleHandler()
        handler.handle(_make_event(EventType.INCIDENT_UPDATED, previous="Investigating"))
        output = capsys.readouterr().out
        assert "UPDATED" in output
        assert "Previous: Investigating" in output

    def test_prints_resolved(self, capsys):
        handler = ConsoleHandler()
        handler.handle(_make_event(EventType.INCIDENT_RESOLVED, status="Resolved"))
        output = capsys.readouterr().out
        assert "RESOLVED" in output


class TestRunConsumer:
    @pytest.mark.asyncio
    async def test_processes_events_until_shutdown(self):
        bus = EventBus()
        handled = []

        class RecordingHandler:
            def handle(self, event):
                handled.append(event)

        await bus.emit(_make_event(title="One"))
        await bus.emit(_make_event(title="Two"))
        await bus.shutdown()

        await run_consumer(bus, RecordingHandler())

        assert len(handled) == 2
        assert handled[0].incident.title == "One"
        assert handled[1].incident.title == "Two"
