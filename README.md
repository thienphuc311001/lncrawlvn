# lncrawl-mini

Minimal web-novel downloader built from the zh + vn sources of
[Lightnovel Crawler](https://github.com/lncrawl/lightnovel-crawler). No server,
no database, no account system. One URL in → a persistent on-disk library you
can read, complete, and export to EPUB/TXT.

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
| `/api/config` | GET | Engine settings with metadata (type, min/max, label, current value) |
| `/api/config` | POST | Update engine settings (max_sessions_per_exit, max_attempts, solve_timeout, use_archive, impersonate, browser_mode, …); persisted to `~/.lncrawl-mini/settings.json` |
| `/api/extract` | POST | Start a crawl job: `{ "url": "...", "first": 5, "workers": 4, "rate_limit": 1.5 }` → `202` + job (or the finished job with `"sync": true`). Every crawl is saved to the library by default (`"save": false` opts out); `workers` (clamped to the source's own ceiling) controls chapter concurrency; `"overwrite": true` replaces already-saved chapters with the re-crawled bodies (repair truncated copies) |
| `/api/jobs/{job_id}` | GET | Job progress: status, timestamped stage logs, per-chapter success/failure + reasons |
| `/api/books` | GET | All books in the library with saved/total chapter counts |
| `/api/books/{book_id}` | GET | Book metadata + full TOC annotated with per-chapter saved/missing flags |
| `/api/books/{book_id}/cover` | GET | Downloaded cover image (404 if none) |
| `/api/books/{book_id}/chapters/{n}` | GET | One saved chapter body (HTML) |
| `/api/books/{book_id}/chapters/{n}` | DELETE | Remove one saved chapter file (TOC entry kept) → `204`; re-download later via fetch-missing (`409` while a crawl job is running for the book) |
| `/api/books/{book_id}/fetch-missing` | POST | Start a job that downloads only chapters missing on disk → `202` + job |
| `/api/books/{book_id}` | DELETE | Remove a book with all saved chapters, cover, and exports → `204` (`409` while a crawl job is running for it) |
| `/api/books/{book_id}/export?format=epub\|txt` | GET | Build an EPUB/TXT from saved chapters and download it as a ZIP |


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
- **No DB** — the library is plain JSON files under `~/.lncrawl-mini/library/`:
  `book.json` (metadata + TOC), `cover.jpg`, and one `chapters/0001-0100/ch_0001.json`
  file per fetched chapter, written atomically the moment each download finishes
  (crash-safe).

- **No search** — crawl by URL only。
- **Anti-bot reused** — HTTP/Cloudflare/browser escalation come from the
  `lncrawl-scraper` package unchanged; this repo only wires defaults.