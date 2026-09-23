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
| `/api/books/{book_id}/export?format=epub\|txt&per_file=100` | GET | Build an EPUB/TXT and download it as a ZIP — one `0001-0010.epub`-style file (chapter range only; the ZIP name keeps the title) per `per_file` chapters (default 100, max 10000). Only the first file carries the novel metadata header, intro page, and cover |


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
| `--safe-block-target N` | Start looking for a clean TXT block boundary here (default 2600 chars) |
| `--safe-block-max N` | Hard limit for one TXT block (default 3000 chars) |
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
## Chinese → Vietnamese translation

The translation feature accepts UTF-8 RAW Chinese and an optional cumulative book dictionary.

~~~sh
./scripts/translate.sh --raw '/path/to/raw.txt' --dictionary '/path/to/dictionary.json' --output translation-output
~~~

Omit --dictionary for the first batch. --check-inputs validates numbered Chinese chapter headings without API requests; --first N selects a prefix. Gaps are allowed. Long dashed export separators are ignored; duplicate and backwards headings are errors. Repeat the same command to resume a batch. The CLI and browser share atomic checkpoints.

Set GOOGLE_AI_API_KEY in the project-root .env. Optional GOOGLE_AI_API_KEY_BACKUP, GOOGLE_AI_API_KEY_THIRD, or comma-separated GOOGLE_AI_API_KEYS add key slots. The primary model is gemini-3.1-flash-lite and the fallback is gemini-3.5-flash-lite. Two workers with 300 ms request staggering are the default. Daily quota exhaustion disables one key/model pair for the run; RPM/TPM limits cool that pair temporarily. TRANSLATION_RETRIES_PER_PAIR defaults to two retries.

Before translation, a local RAW scan finds terminology candidates. Gemini proposes confirmed entry/alias patches and unresolved items; local code validates and merges them. An ordinary chapter fits in one translation request. Long chapters use paragraph and estimated output-token budgets, with stable paragraph IDs, previous accepted Vietnamese context, and one RAW lookahead paragraph. Local validation checks IDs, numbers, Chinese residue, locked terms, and truncation before an atomic chapter commit. Only suspicious content receives targeted AI QA; confirmed defects get one complete-paragraph repair.

Completed batches include translated.txt and translated.json for Vietnamese chapters, dictionary.json for the full merged cumulative book dictionary, and unresolved.json for candidates needing review.

The exported `dictionary.json` is a versioned object containing the full locked `entries` array and carry-forward `unresolved` records, so the same file can be supplied to the next batch. Flat entry arrays remain accepted as inputs. Each entry has exactly source, translation, type, gender, status=locked, and aliases; aliases have source and translation. Canonical types are character, location, institution, book_title, memorial, book_section, and term. Older organization, ability, item, and concept types migrate explicitly to institution or term; a v10 dictionary cannot contain legacy types. Gender is male, female, unknown, or not_applicable (required for non-person entries). `unresolved.json` is also exported for review. Versioned older dictionaries remain accepted as input and produce a migration record.

~~~json
{
  "version": 10,
  "entries": [{
    "source": "邱途",
    "translation": "Khâu Đồ",
    "type": "character",
    "gender": "male",
    "status": "locked",
    "aliases": [{"source": "邱探员", "translation": "Thám viên Khâu"}]
  }],
  "unresolved": []
}
~~~

The API exposes POST /api/translation/jobs with {raw, dictionary?}, saved-job list/detail/resume/cancel/delete routes, and output downloads under /api/translation/jobs/{id}/outputs/. Parser errors include input, error_type, chapter, line, previous_line, and reason. Request logs record requested and actual models, key slots, finish reasons, retries, token counts when available, and QA outcomes. API keys are never written to checkpoints.

`TRANSLATION_AUTHOR_NOTE_POLICY` controls clearly marked author/platform notes: `preserve` (default), `remove` from reader output, or `separate` into `author-notes.txt`. All source IDs remain in the internal chapter artifact. Request-budget warnings and a hard guard stop runaway jobs; per-operation counts appear in the browser and batch result.

See [the translation pipeline design](docs/translation-pipeline.md) for details.
