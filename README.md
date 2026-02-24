# OpenAI Status Tracker

Monitors the OpenAI status page for service incidents and logs them in real-time. Built with an event-driven internal architecture that scales to 100+ status pages.

## Quick Start

```bash
pip install -r requirements.txt
python -m status_tracker
```

Web dashboard runs at `http://localhost:8080` by default. Console output runs in parallel.

## Usage

```bash
# Default: monitor OpenAI
python -m status_tracker

# Custom pages via CLI
python -m status_tracker --pages OpenAI=https://status.openai.com GitHub=https://www.githubstatus.com

# Custom intervals and concurrency
python -m status_tracker --poll-interval 30 --max-poll-interval 120 --concurrency 100

# From config file
python -m status_tracker --config config.json

# Console only (no web server)
python -m status_tracker --no-web

# Custom web port
python -m status_tracker --port 3000
```

### Config file format

```json
{
  "pages": [
    {"name": "OpenAI", "base_url": "https://status.openai.com"},
    {"name": "GitHub", "base_url": "https://www.githubstatus.com"},
    {"name": "Stripe", "base_url": "https://status.stripe.com", "poll_interval": 90}
  ]
}
```

## Running Tests

```bash
pytest tests/ -v
```

22 tests covering parser correctness, event bus behavior, and full integration tests with a mock feed server (new incident → update → resolve → error recovery).

---

## Investigation: What Does status.openai.com Actually Expose?

Before writing any code, I inspected what `status.openai.com` actually offers. It's powered by **incident.io** (not Atlassian Statuspage, which is a common assumption).

### Endpoints explored

| Endpoint | Format | What I found |
|---|---|---|
| `/feed.atom` | Atom XML | Full incident data: title, status, timestamps, affected components embedded in HTML `<content>`. Standard Atom format — works with any provider. |
| `/feed.rss` | RSS XML | Same data in RSS format. Atom is slightly richer (has `<updated>` per-entry), so I went with Atom. |
| `/api/v2/incidents.json` | JSON | Structured but `body` fields are empty strings. incident.io-specific — not portable. |
| `/api/v2/summary.json` | JSON | Lists components but no incident data. Useless for monitoring. |

### "Subscribe to updates" options

The status page offers three subscription methods:

- **Email** — OpenAI pushes emails on incidents. Would require standing up an email receiver + parsing HTML emails. Overkill for this use case, and doesn't scale to 100+ arbitrary pages.
- **RSS/Atom** — Just gives you the feed URL. Your reader polls it. No push involved.
- **Slack** — Posts to a Slack channel via webhook. Requires Slack workspace setup per provider. Not portable.

### Is there a push mechanism?

I checked the Atom feed for a [WebSub](https://www.w3.org/TR/websub/) hub (`<link rel="hub">`), which would enable real-time push notifications via the Atom standard. **There isn't one.** The feed is plain Atom with no push signaling.

No SSE endpoint. No WebSocket. No public webhook registration API.

**The Atom feed is the only viable machine-readable interface**, and polling it is the only option for consumer-side monitoring.

---

## Design Decisions

### Why polling is the right answer here

The problem statement asks for an "event-based approach." I want to be explicit: **there is no way to get push notifications from OpenAI's status page** without infrastructure they don't expose (webhook admin access, a WebSub hub, SSE/WS endpoints). This isn't an oversight — I investigated every option.

What I built instead: **efficient change detection on top of polling, with an event-driven internal architecture.** The polling is the data acquisition layer. Everything downstream is event-driven — monitors produce `StatusEvent`s into an async queue, consumers (console, web dashboard) react independently.

The polling itself is optimized to be as close to push as possible:

1. **HTTP conditional requests (ETag / If-Modified-Since)** — each poll sends cached headers. If nothing changed, the server returns `304 Not Modified` — zero body transfer. At 100+ pages, the vast majority of polls are 304s.

2. **Feed-level short-circuit** — compare the feed's `<updated>` timestamp before parsing any entries. Skip all XML work if unchanged.

3. **Entry-level dedup** — track `(entry_id, updated_timestamp)` per incident. Only actual state changes emit events.

4. **Backoff on errors only** — transient failures get geometric backoff with jitter. Successful polls always run at the configured interval. I deliberately chose not to back off on "no changes" — quiet periods shouldn't increase detection latency.

### Why stdlib XML over feedparser

[feedparser](https://github.com/kurtmckee/feedparser) is the standard Python library for RSS/Atom parsing — actively maintained, 2k+ stars, solid choice. I chose not to use it here because:

- The feed structure I'm parsing is narrow: one provider type (incident.io), one format (Atom), with a known HTML content pattern. `xml.etree.ElementTree` (stdlib) handles this in ~60 lines.
- The custom `HTMLParser` subclass I wrote extracts status text and affected components from the `<content>` HTML — feedparser wouldn't help with that part anyway, since the incident-specific structure is custom to incident.io.
- Keeping it to one external dependency (`aiohttp`) means fewer things to audit and version-pin.

feedparser would be the right call if I needed to handle diverse feed formats (RSS 0.9x, RSS 2.0, Atom 0.3, Atom 1.0, CDF) from unknown providers. For this use case, it's unnecessary.

The tricky part is extracting status and components from the HTML inside `<content>`. I wrote a small `HTMLParser` subclass that pulls `<b>Status: Investigating</b>` and `<li>Conversations (Degraded Performance)</li>` patterns. It's ~40 lines and handles everything the real feed throws at it.

### Alternatives I rejected

| Approach | Why not |
|---|---|
| **feedparser library** | Good library, but adds a dependency for a feed narrow enough that stdlib handles it. Doesn't help with the custom HTML parsing anyway. |
| **Selenium/Playwright scraping** | Brittle, slow, likely blocked by ToS, doesn't scale |
| **Statuspage JSON API** (`/api/v2/`) | Empty `body` fields, incident.io-specific, not portable across providers |
| **Webhook receiver + ngrok** | Would be truly event-based but requires infrastructure beyond scope, and not all providers offer webhooks |
| **Email subscription parsing** | OpenAI offers email alerts, but standing up an email receiver for 100+ providers is not practical |

---

## Observations from the Real Feed

Things I discovered by running against the live `status.openai.com/feed.atom`:

- **Brotli encoding**: The server returns `Content-Encoding: br` by default. aiohttp can't decode Brotli without the optional `brotli` package. I explicitly request `Accept-Encoding: gzip, deflate` to avoid this — one fewer dependency.

- **Double-slash in links**: Incident URLs in the feed contain `//incidents/` (e.g., `status.openai.com//incidents/01KHYH...`). This is in the feed itself, not a parsing bug. I left it as-is rather than silently "fixing" upstream data.

- **HTML content is XML-escaped**: The `<content type="html">` field contains HTML escaped as `&lt;b&gt;Status: Resolved&lt;/b&gt;`. ElementTree handles this correctly — `content_el.text` returns the unescaped HTML. I discovered this when my mock test server served unescaped HTML and the parser got `None` for content.

- **~50 historical entries on first fetch**: The feed includes incidents going back months. Without silent seeding on first poll, startup would spam dozens of old resolved incidents. The monitor seeds `_known` on the first poll without emitting events.

- **Feed generator is `incident.io`**: Confirmed via `<generator>incident.io</generator>`. This is relevant because incident.io feeds have slightly different structure than Atlassian Statuspage feeds (e.g., `<summary>` vs `<content>` for incident details).

---

## Architecture

```
FeedMonitor(s) ──emit──▸ EventBus (asyncio.Queue) ──recv──▸ ConsoleHandler
  (1 per page)                                              ──recv──▸ WebHandler (dashboard)
```

- **FeedMonitor**: One per page. Async HTTP fetch with ETag caching, retry with exponential backoff + jitter, Atom parsing, change detection.
- **EventBus**: `asyncio.Queue` decoupling producers from consumers. Non-blocking emit — drops events on overflow rather than deadlocking producers.
- **ConsoleHandler**: ANSI color-coded terminal output (red = new, yellow = update, green = resolved).
- **WebHandler**: In-memory event buffer serving an HTML dashboard. Auto-refreshes every 30s.

Single `asyncio` event loop with tuned `TCPConnector` — scales to 100+ pages with no threads.

### Scalability to 100+ pages

| Feature | Impact |
|---|---|
| ETag/If-Modified-Since | Most polls return 304 — near-zero bandwidth |
| Semaphore-bounded concurrency | Configurable via `--concurrency` |
| TCPConnector with per-host limits | Prevents overwhelming any single provider |
| DNS cache (300s TTL) | Reduces DNS lookups across monitors |
| Retry with exponential backoff + jitter | Prevents thundering herd on transient failures |
| Per-monitor isolation | One page's parse error doesn't crash others |

### How it would evolve for true push

The `EventBus` abstraction means the data source is swappable:

- **Webhook relay**: For providers with outbound webhooks (some enterprise Statuspage plans), receive POSTs into a queue that feeds the same `EventBus → Handler` pipeline. The polling loop is replaced; everything downstream stays the same.
- **WebSub**: If a provider's Atom feed advertises a WebSub hub (none do currently), subscribe for real-time push instead of polling.
- **Hybrid**: Use webhooks where available, fall back to polling for the rest. Handlers don't care about the source — they just consume `StatusEvent`s.

## Deployment

### Render

The app is deployed at: **https://openai-status-tracker-kz6k.onrender.com**

To deploy your own:
1. Push to GitHub
2. Connect repo in [Render dashboard](https://dashboard.render.com)
3. Auto-detects `Dockerfile` and `render.yaml`
4. Deploy

Render sets `PORT` automatically. The free tier spins down after 15 min of inactivity — the app re-seeds on restart.

### Docker

```bash
docker build -t status-tracker .
docker run -p 8080:8080 status-tracker
```

### Console only (no web server)

```bash
python -m status_tracker --no-web
```
