from __future__ import annotations

import html
import os
from collections import deque
from datetime import datetime
from string import Template

from aiohttp import web

from status_tracker.models import EventType, StatusEvent

MAX_EVENTS = 100

HTML_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Status Tracker</title>
<meta http-equiv="refresh" content="30">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f9fafb; color: #1f2937; max-width: 800px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: #6b7280; font-size: 0.875rem; margin-bottom: 24px; }
  .empty { text-align: center; color: #9ca3af; padding: 60px 20px; }
  .event { border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 12px;
           border-left: 4px solid; }
  .event .badge { display: inline-block; font-size: 0.75rem; font-weight: 600;
                  padding: 2px 8px; border-radius: 4px; margin-bottom: 8px; }
  .event .title { font-weight: 600; font-size: 1rem; margin-bottom: 4px; }
  .event .meta { font-size: 0.8rem; color: #6b7280; }
  .event .meta span { margin-right: 16px; }
  .event a { color: #2563eb; text-decoration: none; }
  .event a:hover { text-decoration: underline; }
  .new { border-left-color: #ef4444; }
  .new .badge { background: #fee2e2; color: #991b1b; }
  .updated { border-left-color: #eab308; }
  .updated .badge { background: #fef9c3; color: #854d0e; }
  .resolved { border-left-color: #22c55e; }
  .resolved .badge { background: #dcfce7; color: #166534; }
</style>
</head>
<body>
<h1>Status Tracker</h1>
<p class="subtitle">Monitoring $page_count page(s) &middot; Last $event_count events &middot; Auto-refreshes every 30s</p>
$content
</body>
</html>""")


class WebHandler:
    """Accumulates StatusEvents and serves an HTML dashboard."""

    def __init__(self) -> None:
        self._events: deque[StatusEvent] = deque(maxlen=MAX_EVENTS)
        self._page_count = 0

    def set_page_count(self, count: int) -> None:
        self._page_count = count

    def handle(self, event: StatusEvent) -> None:
        self._events.appendleft(event)

    async def index(self, request: web.Request) -> web.Response:
        if not self._events:
            content = '<p class="empty">No events yet. Monitoring in progress...</p>'
        else:
            content = "\n".join(self._render_event(e) for e in self._events)

        page = HTML_TEMPLATE.substitute(
            page_count=self._page_count,
            event_count=len(self._events),
            content=content,
        )
        return web.Response(text=page, content_type="text/html")

    @staticmethod
    def _render_event(event: StatusEvent) -> str:
        inc = event.incident
        et = event.event_type

        if et == EventType.NEW_INCIDENT:
            css_class, label = "new", "NEW INCIDENT"
        elif et == EventType.INCIDENT_UPDATED:
            css_class, label = "updated", "UPDATED"
        else:
            css_class, label = "resolved", "RESOLVED"

        components = ""
        if inc.affected_components:
            components = ", ".join(f"{html.escape(c.name)} ({html.escape(c.status)})" for c in inc.affected_components)

        updated_str = inc.updated.isoformat() if isinstance(inc.updated, datetime) else str(inc.updated)

        meta_parts = [f"<span>Status: <b>{html.escape(inc.status_text or 'Unknown')}</b></span>"]
        if event.previous_status:
            meta_parts.append(f"<span>Previous: {html.escape(event.previous_status)}</span>")
        if components:
            meta_parts.append(f"<span>Affected: {components}</span>")
        meta_parts.append(f"<span>{html.escape(updated_str)}</span>")

        link = ""
        if inc.link:
            link = f' &middot; <a href="{html.escape(inc.link)}" target="_blank">View incident</a>'

        return f"""\
<div class="event {css_class}">
  <span class="badge">{label}</span> <span style="color:#6b7280;font-size:0.8rem">{html.escape(event.page_name)}</span>
  <div class="title">{html.escape(inc.title)}{link}</div>
  <div class="meta">{"".join(meta_parts)}</div>
</div>"""


def create_web_app(handler: WebHandler) -> web.Application:
    app = web.Application()
    app.router.add_get("/", handler.index)
    return app
