from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EventType(Enum):
    NEW_INCIDENT = "NEW_INCIDENT"
    INCIDENT_UPDATED = "INCIDENT_UPDATED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"


@dataclass(frozen=True)
class AffectedComponent:
    name: str
    status: str


@dataclass(frozen=True)
class Incident:
    id: str
    title: str
    updated: datetime
    status_text: str
    message: str
    affected_components: tuple[AffectedComponent, ...] = ()
    link: str = ""


@dataclass(frozen=True)
class StatusEvent:
    event_type: EventType
    page_name: str
    incident: Incident
    previous_status: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class PageConfig:
    name: str
    base_url: str
    feed_path: str = "/feed.atom"
    poll_interval: float = 60.0
    max_poll_interval: float = 300.0
    backoff_factor: float = 1.5
