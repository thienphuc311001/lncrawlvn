# lncrawl-mini

Minimal web-novel downloader built from the zh + vn sources of
[Lightnovel Crawler](https://github.com/lncrawl/lightnovel-crawler). No server,
no database, no account system, no web UI. One URL in → EPUB + TXT out.

## Development

Run the FastAPI backend (port 8000) and the Next.js frontend (port 3000) with a
single command:

```bash
lnmini run dev
```

- Backend API: http://127.0.0.1:8000 — interactive docs at http://127.0.0.1:8000/docs
- Frontend: http://localhost:3000
- `Ctrl+C` stops both processes.

`lnmini run backend` and `lnmini run frontend` start a single side. The
installer lives at `scripts/lnmini.sh`; copy it anywhere on your `PATH` as
`lnmini` (e.g. `~/.local/bin/lnmini`) if it is not installed yet.

### API

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/health` | GET | Liveness probe |
| `/api/extract` | POST | Start a crawl job: `{ "url": "...", "first": 5 }` → `202` + job (or the finished job with `"sync": true`) |
| `/api/jobs/{job_id}` | GET | Job progress: status, timestamped stage logs, per-chapter success/failure + reasons |


## Usage

```bash
uv run python -m lncrawl "https://example.com/novel/url"
```

Options:

| Flag | Meaning |
| --- | --- |
| `-o DIR` | Output directory (defaults to `~/.lncrawl-mini/light_novels`) |
| `--first N` | Download only the first N chapters |
| `--last N` | Download only the last N chapters |
| `--rate-limit N` | Limit crawl to N requests/second on this site |
| `--no-cover` | Skip cover download/generation |
| `-v` | Debug logging |

## Sources

Only the crawlers under `sources/zh/` and `sources/vi/` are loaded. Every other
source corpus from the original project is intentionally absent.

## How it stays simple

- **No `ctx` service graph** — just a logger, one shared scraper state, one source registry.
- **No DB** — chapters live in memory and are bound straight into the output files.

- **No search** — crawl by URL only。
- **Anti-bot reused** — HTTP/Cloudflare/browser escalation come from the
  `lncrawl-scraper` package unchanged; this repo only wires defaults.