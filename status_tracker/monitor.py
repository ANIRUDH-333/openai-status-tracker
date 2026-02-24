from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime

import aiohttp

from status_tracker.events import EventBus
from status_tracker.models import EventType, Incident, PageConfig, StatusEvent
from status_tracker.parser import parse_atom_feed

logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds


class FeedMonitor:
    """Watches a single Atom feed for incident changes."""

    def __init__(
        self,
        config: PageConfig,
        event_bus: EventBus,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        on_seed: Callable[[str, list[Incident]], None] | None = None,
    ) -> None:
        self._config = config
        self._bus = event_bus
        self._session = session
        self._semaphore = semaphore
        self._on_seed = on_seed

        # Change detection state
        self._known: dict[str, tuple[datetime, str]] = {}  # entry_id → (updated, status_text)
        self._last_feed_updated: datetime | None = None
        self._current_interval = config.poll_interval
        self._seeded = False

        # HTTP conditional request headers (ETag / Last-Modified)
        self._etag: str | None = None
        self._last_modified: str | None = None

        # Metrics
        self.polls_total = 0
        self.polls_304 = 0
        self.polls_failed = 0
        self.events_emitted = 0

    @property
    def feed_url(self) -> str:
        return f"{self._config.base_url.rstrip('/')}{self._config.feed_path}"

    async def run(self) -> None:
        """Main polling loop. Exits cleanly on cancellation."""
        logger.info("Monitoring %s at %s", self._config.name, self.feed_url)
        await self._poll()
        while True:
            await asyncio.sleep(self._current_interval)
            await self._poll()

    async def _fetch_feed(self) -> tuple[bytes | None, bool]:
        """Fetch the Atom feed with bounded concurrency, ETag/If-Modified-Since, and retry.

        Returns (body, was_304). body is None on error, was_304=True means unchanged.
        """
        # Explicitly avoid Brotli — status.openai.com returns Content-Encoding: br by default,
        # and aiohttp can't decode it without the optional `brotli` package.
        headers: dict[str, str] = {"Accept-Encoding": "gzip, deflate"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        for attempt in range(MAX_RETRIES):
            async with self._semaphore:
                try:
                    async with self._session.get(
                        self.feed_url,
                        timeout=aiohttp.ClientTimeout(total=30),
                        headers=headers,
                    ) as resp:
                        if resp.status == 304:
                            self.polls_304 += 1
                            return None, True

                        if resp.status != 200:
                            logger.warning("%s: HTTP %d from %s", self._config.name, resp.status, self.feed_url)
                            if resp.status >= 500 and attempt < MAX_RETRIES - 1:
                                await self._retry_delay(attempt)
                                continue
                            return None, False

                        # Cache conditional headers for next request
                        if "ETag" in resp.headers:
                            self._etag = resp.headers["ETag"]
                        if "Last-Modified" in resp.headers:
                            self._last_modified = resp.headers["Last-Modified"]

                        return await resp.read(), False

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning("%s: fetch error (attempt %d/%d): %s", self._config.name, attempt + 1, MAX_RETRIES, e)
                    if attempt < MAX_RETRIES - 1:
                        await self._retry_delay(attempt)
                        continue
                    return None, False

        return None, False

    @staticmethod
    async def _retry_delay(attempt: int) -> None:
        """Exponential backoff with jitter for retries."""
        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
        await asyncio.sleep(delay)

    async def _poll(self) -> None:
        """Single poll cycle: fetch → parse → detect changes → emit events."""
        self.polls_total += 1
        body, was_304 = await self._fetch_feed()

        if was_304:
            # Server confirmed nothing changed — no need to parse or backoff
            return

        if body is None:
            self.polls_failed += 1
            self._backoff()
            return

        try:
            feed_updated, incidents = parse_atom_feed(body)
        except Exception:
            logger.exception("%s: failed to parse feed", self._config.name)
            self.polls_failed += 1
            self._backoff()
            return

        # Feed-level short-circuit
        if feed_updated and self._last_feed_updated and feed_updated <= self._last_feed_updated:
            return

        self._last_feed_updated = feed_updated
        await self._detect_changes(incidents)

        # Prune _known to only IDs in the current feed (prevents unbounded growth)
        current_ids = {inc.id for inc in incidents}
        self._known = {k: v for k, v in self._known.items() if k in current_ids}

    async def _detect_changes(self, incidents: list[Incident]) -> None:
        """Compare incidents against known state, emit events for changes."""
        is_first_poll = not self._seeded

        for inc in incidents:
            if not inc.id:
                continue

            prev = self._known.get(inc.id)
            if prev is None:
                # Seed silently on first poll — the feed contains ~50 historical entries
                # going back months. Without this, startup would spam old resolved incidents.
                self._known[inc.id] = (inc.updated, inc.status_text)
                if not self._seeded:
                    continue
                event_type = EventType.NEW_INCIDENT
                if inc.status_text.lower() in ("resolved", "postmortem"):
                    event_type = EventType.INCIDENT_RESOLVED
                await self._bus.emit(
                    StatusEvent(
                        event_type=event_type,
                        page_name=self._config.name,
                        incident=inc,
                    )
                )
                self.events_emitted += 1
            else:
                prev_updated, prev_status = prev
                if inc.updated != prev_updated or inc.status_text != prev_status:
                    self._known[inc.id] = (inc.updated, inc.status_text)
                    if inc.status_text.lower() in ("resolved", "postmortem"):
                        event_type = EventType.INCIDENT_RESOLVED
                    else:
                        event_type = EventType.INCIDENT_UPDATED
                    await self._bus.emit(
                        StatusEvent(
                            event_type=event_type,
                            page_name=self._config.name,
                            incident=inc,
                            previous_status=prev_status,
                        )
                    )
                    self.events_emitted += 1

        self._seeded = True

        # After first poll, notify callback with historical incidents for dashboard seeding
        if is_first_poll and self._on_seed:
            self._on_seed(self._config.name, incidents)

    def _backoff(self) -> None:
        self._current_interval = min(
            self._current_interval * self._config.backoff_factor,
            self._config.max_poll_interval,
        )
