from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from status_tracker.models import EventType, StatusEvent

logger = logging.getLogger(__name__)

# ANSI color codes
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_RESET = "\033[0m"
_BOLD = "\033[1m"

_EVENT_STYLE: dict[EventType, tuple[str, str]] = {
    EventType.NEW_INCIDENT: (_RED, "NEW INCIDENT"),
    EventType.INCIDENT_UPDATED: (_YELLOW, "UPDATED"),
    EventType.INCIDENT_RESOLVED: (_GREEN, "RESOLVED"),
}


class EventBus:
    """Async event bus backed by an asyncio.Queue."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[StatusEvent | None] = asyncio.Queue(maxsize=maxsize)

    async def emit(self, event: StatusEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping event: %s %s", event.event_type.value, event.incident.title)

    async def shutdown(self) -> None:
        """Signal consumers to stop."""
        await self._queue.put(None)

    async def consume(self) -> StatusEvent | None:
        """Get the next event, or None if shutting down."""
        return await self._queue.get()


class ConsoleHandler:
    """Prints StatusEvents to the console with ANSI colors."""

    def handle(self, event: StatusEvent) -> None:
        color, label = _EVENT_STYLE.get(event.event_type, (_RESET, "EVENT"))
        inc = event.incident

        components_str = ""
        if inc.affected_components:
            components_str = ", ".join(f"{c.name} ({c.status})" for c in inc.affected_components)

        updated_str = inc.updated.isoformat() if isinstance(inc.updated, datetime) else str(inc.updated)

        lines = [
            f"{color}{_BOLD}[{label}]{_RESET} {event.page_name}",
            f"  Incident: {inc.title}",
            f"  Status:   {inc.status_text or 'Unknown'}",
        ]
        if event.previous_status:
            lines.append(f"  Previous: {event.previous_status}")
        if components_str:
            lines.append(f"  Affected: {components_str}")
        lines.append(f"  Updated:  {updated_str}")
        if inc.link:
            lines.append(f"  Link:     {inc.link}")

        print("\n".join(lines))
        print()


async def run_consumer(bus: EventBus, handler: ConsoleHandler) -> None:
    """Consume events from the bus and pass them to the handler."""
    while True:
        event = await bus.consume()
        if event is None:
            break
        try:
            handler.handle(event)
        except Exception:
            logger.exception("Error handling event")
