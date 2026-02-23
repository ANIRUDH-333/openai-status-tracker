# OpenAI Status Tracker

Automatically detects and logs service incidents from the OpenAI Status Page (and any Atom-compatible status page) using an event-driven architecture.

## Quick Start

```bash
pip install -r requirements.txt
python -m status_tracker
```

## Usage

```bash
# Default: monitor OpenAI
python -m status_tracker

# Custom pages via CLI
python -m status_tracker --pages OpenAI=https://status.openai.com GitHub=https://www.githubstatus.com

# Custom intervals
python -m status_tracker --poll-interval 30 --max-poll-interval 120

# From config file
python -m status_tracker --config config.json

# Adjust concurrency for 100+ pages
python -m status_tracker --config pages.json --concurrency 100
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

## Design Decisions

### Why polling? Why not true push-based events?

The OpenAI status page (powered by incident.io) does not expose push-based notification mechanisms — no WebSocket endpoint, no Server-Sent Events (SSE), and no public webhook registration. The Atom feed at `/feed.atom` is the only machine-readable interface available.

This is true for virtually all status page providers (incident.io, Atlassian Statuspage, etc.). They publish Atom/RSS feeds but don't offer consumer-facing push APIs.

**Given this constraint, we built an efficient adaptive polling system that minimizes overhead:**

1. **HTTP conditional requests (ETag / If-Modified-Since)**: On each poll, we send cached `ETag` and `Last-Modified` headers. If the feed hasn't changed, the server returns `304 Not Modified` — zero body transfer, minimal bandwidth. At 100+ pages, most polls are 304s.

2. **Feed-level short-circuit**: Even if we get a full response, we compare the feed's `<updated>` timestamp before parsing entries. If unchanged, we skip all XML processing.

3. **Entry-level dedup**: We track `(entry_id, updated_timestamp)` per incident. Only actual state changes (new incident, status change, resolution) produce events.

4. **Adaptive backoff on errors only**: Transient failures trigger geometric backoff with jitter (60s → 90s → 135s → ... → 300s max). Successful polls always run at the configured interval — we don't penalize quiet periods.

### How it would evolve for true push

For providers that support it, the architecture is ready:

- **Webhook relay**: Some enterprise status page plans offer outbound webhooks. These could post to a message queue (SQS, Redis Streams), which a consumer would read from — replacing the polling loop but keeping the same `EventBus → Handler` pipeline.
- **WebSub/PubSubHubbub**: The Atom spec supports WebSub for real-time push. If a status page advertises a WebSub hub, we could subscribe instead of polling.
- **Hybrid approach**: Use webhooks where available, fall back to polling for pages that don't support them. The `EventBus` abstraction means handlers don't care about the source.

## Architecture

```
FeedMonitor(s) ──emit──▸ EventBus (asyncio.Queue) ──recv──▸ ConsoleHandler
  (1 per page)                                              (+ future: Slack, DB, webhook)
```

- **FeedMonitor**: One per page. Async HTTP fetch with ETag caching, retry with jitter, Atom parsing, change detection.
- **EventBus**: `asyncio.Queue` decoupling producers from consumers. Backpressure-aware (drops events on overflow rather than blocking producers).
- **ConsoleHandler**: Formats and prints events with ANSI colors (red = new, yellow = update, green = resolved).

Single `asyncio` event loop with tuned `TCPConnector` — scales to 100+ pages with no threads.

### Scalability features

| Feature | Impact at 100+ pages |
|---|---|
| ETag/If-Modified-Since | Most polls return 304 — near-zero bandwidth |
| Semaphore-bounded concurrency | Configurable via `--concurrency` |
| TCPConnector with per-host limits | Prevents overwhelming any single provider |
| DNS cache (300s TTL) | Reduces DNS lookups across monitors |
| Retry with exponential backoff + jitter | Prevents thundering herd on transient failures |
| Per-monitor isolation | One page's error doesn't affect others |

