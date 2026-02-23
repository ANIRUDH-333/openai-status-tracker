from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

from status_tracker.models import AffectedComponent, Incident

ATOM_NS = "http://www.w3.org/2005/Atom"


class _ContentHTMLParser(HTMLParser):
    """Extract status text and affected components from Atom entry HTML content.

    Expected patterns in the HTML:
      <b>Status: Investigating</b>
      <li>Conversations (Degraded Performance)</li>
    """

    def __init__(self) -> None:
        super().__init__()
        self.status_text: str = ""
        self.components: list[AffectedComponent] = []
        self._in_bold = False
        self._in_li = False
        self._current_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "b":
            self._in_bold = True
            self._current_text = ""
        elif tag == "li":
            self._in_li = True
            self._current_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "b" and self._in_bold:
            self._in_bold = False
            text = self._current_text.strip()
            if text.lower().startswith("status:"):
                self.status_text = text.split(":", 1)[1].strip()
        elif tag == "li" and self._in_li:
            self._in_li = False
            self._parse_component(self._current_text.strip())

    def handle_data(self, data: str) -> None:
        if self._in_bold or self._in_li:
            self._current_text += data

    def _parse_component(self, text: str) -> None:
        # "Conversations (Degraded Performance)" → name="Conversations", status="Degraded Performance"
        if "(" in text and text.endswith(")"):
            paren_start = text.rfind("(")
            name = text[:paren_start].strip()
            status = text[paren_start + 1 : -1].strip()
            if name:
                self.components.append(AffectedComponent(name=name, status=status))
        elif text:
            self.components.append(AffectedComponent(name=text, status="Unknown"))


def _extract_content(html: str) -> tuple[str, str, list[AffectedComponent]]:
    """Parse HTML content from an Atom entry. Returns (status_text, message, components)."""
    parser = _ContentHTMLParser()
    parser.feed(html)
    # Use the raw HTML stripped of tags as the message fallback
    message = html.strip()
    return parser.status_text, message, parser.components


def _parse_datetime(text: str) -> datetime:
    """Parse an ISO 8601 datetime string from Atom feeds. Always returns timezone-aware."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_atom_feed(xml_bytes: bytes) -> tuple[datetime | None, list[Incident]]:
    """Parse an Atom feed into a list of Incidents.

    Returns (feed_updated_timestamp, incidents).
    The feed_updated_timestamp can be used for short-circuit comparison.
    """
    root = ET.fromstring(xml_bytes)

    # Feed-level <updated> for short-circuit
    feed_updated_el = root.find(f"{{{ATOM_NS}}}updated")
    feed_updated = _parse_datetime(feed_updated_el.text) if feed_updated_el is not None and feed_updated_el.text else None

    incidents: list[Incident] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        entry_id_el = entry.find(f"{{{ATOM_NS}}}id")
        title_el = entry.find(f"{{{ATOM_NS}}}title")
        updated_el = entry.find(f"{{{ATOM_NS}}}updated")
        content_el = entry.find(f"{{{ATOM_NS}}}content")
        link_el = entry.find(f"{{{ATOM_NS}}}link")

        entry_id = entry_id_el.text.strip() if entry_id_el is not None and entry_id_el.text else ""
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        updated = _parse_datetime(updated_el.text) if updated_el is not None and updated_el.text else datetime.now(timezone.utc)

        link = ""
        if link_el is not None:
            link = link_el.get("href", "")

        status_text = ""
        message = ""
        components: list[AffectedComponent] = []
        if content_el is not None and content_el.text:
            status_text, message, components = _extract_content(content_el.text)

        incidents.append(
            Incident(
                id=entry_id,
                title=title,
                updated=updated,
                status_text=status_text,
                message=message,
                affected_components=tuple(components),
                link=link,
            )
        )

    return feed_updated, incidents
