from __future__ import annotations

import html
from datetime import datetime, timezone

from status_tracker.parser import parse_atom_feed


def _make_feed(entries_xml: str, feed_updated: str = "2025-06-01T00:00:00+00:00") -> bytes:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <updated>{feed_updated}</updated>
  {entries_xml}
</feed>""".encode()


def _make_entry(
    entry_id: str,
    title: str,
    updated: str,
    status: str,
    components: list[tuple[str, str]] | None = None,
) -> str:
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
  <link href="https://status.example.com/incidents/{entry_id}"/>
  <content type="html">{content}</content>
</entry>"""


class TestParseAtomFeed:
    def test_parses_feed_updated_timestamp(self):
        xml = _make_feed("", feed_updated="2025-06-15T12:00:00+00:00")
        feed_updated, incidents = parse_atom_feed(xml)
        assert feed_updated == datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert incidents == []

    def test_parses_single_incident(self):
        entry = _make_entry("inc-1", "API down", "2025-06-15T12:00:00+00:00", "Investigating")
        xml = _make_feed(entry)
        _, incidents = parse_atom_feed(xml)
        assert len(incidents) == 1
        assert incidents[0].id == "inc-1"
        assert incidents[0].title == "API down"
        assert incidents[0].status_text == "Investigating"
        assert incidents[0].link == "https://status.example.com/incidents/inc-1"

    def test_parses_affected_components(self):
        entry = _make_entry(
            "inc-2", "Latency spike", "2025-06-15T12:00:00+00:00", "Identified",
            components=[("API", "Degraded Performance"), ("Dashboard", "Operational")],
        )
        xml = _make_feed(entry)
        _, incidents = parse_atom_feed(xml)
        comps = incidents[0].affected_components
        assert len(comps) == 2
        assert comps[0].name == "API"
        assert comps[0].status == "Degraded Performance"
        assert comps[1].name == "Dashboard"
        assert comps[1].status == "Operational"

    def test_parses_multiple_entries(self):
        entries = "\n".join([
            _make_entry("inc-1", "First", "2025-06-15T12:00:00+00:00", "Resolved"),
            _make_entry("inc-2", "Second", "2025-06-15T13:00:00+00:00", "Investigating"),
        ])
        xml = _make_feed(entries)
        _, incidents = parse_atom_feed(xml)
        assert len(incidents) == 2
        assert incidents[0].id == "inc-1"
        assert incidents[1].id == "inc-2"

    def test_handles_z_suffix_datetime(self):
        entry = _make_entry("inc-1", "Test", "2025-06-15T12:00:00Z", "Investigating")
        xml = _make_feed(entry, feed_updated="2025-06-15T12:00:00Z")
        feed_updated, incidents = parse_atom_feed(xml)
        assert feed_updated is not None
        assert feed_updated.tzinfo is not None
        assert incidents[0].updated.tzinfo is not None

    def test_handles_entry_with_no_content(self):
        entry = """\
<entry>
  <id>inc-empty</id>
  <title>No content</title>
  <updated>2025-06-15T12:00:00+00:00</updated>
</entry>"""
        xml = _make_feed(entry)
        _, incidents = parse_atom_feed(xml)
        assert len(incidents) == 1
        assert incidents[0].status_text == ""
        assert incidents[0].affected_components == ()

    def test_handles_empty_feed(self):
        xml = _make_feed("")
        feed_updated, incidents = parse_atom_feed(xml)
        assert incidents == []
