# lncrawl-mini

Minimal web-novel downloader built from the zh + vn sources of
[Lightnovel Crawler](https://github.com/lncrawl/lightnovel-crawler). No server,
no database, no account system. One URL or local EPUB/TXT upload in → a persistent
on-disk library you can read, complete, and export to EPUB/TXT.

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
| `/api/jobs/{job_id}/stop` | POST | Request a cooperative stop; returns `stopping` until in-flight downloads finish, then `cancelled`. Saved chapters remain on disk. |
| `/api/books` | GET | All books in the library with saved/total chapter counts |
| `/api/books/upload` | POST | Multipart `files` containing one or more EPUB/TXT/ZIP uploads (50 MiB combined). Returns `{ books, errors: [{ filename, error }], skipped }`; each EPUB/TXT in a ZIP becomes a separate saved book. Successful siblings remain available when another book fails. |
| `/api/books/{book_id}` | GET | Book metadata + full TOC annotated with per-chapter saved/missing flags |
| `/api/books/{book_id}/jobs` | GET | Active crawl jobs for a book, including jobs still `stopping`; empty once it is safe to delete. |
| `/api/books/{book_id}/translate-titles` | POST | Translate every TOC title in one 100-chapter section with one structured Gemini request; persist translated titles in the TOC and saved chapter metadata |
| `/api/books/{book_id}/cover` | GET | Downloaded cover image (404 if none) |
| `/api/books/{book_id}/chapters/{n}` | GET | One saved chapter body (HTML) |
| `/api/books/{book_id}/dictionary` | GET / POST | Reader dictionary status or import `{ "dictionary": <cumulative JSON> }` for this book; importing a new file replaces its accumulated dictionary |
| `/api/books/{book_id}/chapters/{n}/translation` | GET | Saved Vietnamese chapter, stale/source-term status, and active translation job progress |
| `/api/books/{book_id}/chapters/{n}/translate` | POST | Translate the saved chapter through the existing `/api/translation/jobs` pipeline; `{ "refresh": true }` starts a new job even when a translation exists |
| `/api/books/{book_id}/chapters/{n}` | DELETE | Remove one saved chapter file (TOC entry kept) → `204`; re-download later via fetch-missing (`409` while a crawl job is running for the book) |
| `/api/books/{book_id}/fetch-missing` | POST | Start a job that downloads only chapters missing on disk → `202` + job |
| `/api/books/{book_id}` | DELETE | Remove a book with all saved chapters, cover, and exports → `204` (`409` while a crawl job is running or stopping; stop it from the book page or job console first) |
| `/api/books/{book_id}/export-options?format=epub\|txt` | GET | Check saved chapter headings before export; `needs_chapter_numbers` is true if any exported chapter lacks a visible number (TXT checks body heading; EPUB checks title or body heading) |
| `/api/books/{book_id}/export?format=epub\|txt&per_file=100&include_chapter_number=false` | GET | Build an EPUB/TXT ZIP with one chapter-range file per `per_file` chapters (default 100, max 10000). Only the first file carries front matter and cover. If `include_chapter_number=true`, add `Chương N` only to chapters whose exported heading lacks a number; otherwise leave them unchanged. The UI asks about this option only when needed. |
| `/api/books/{book_id}/download?format=epub\|txt` | GET | Download all saved chapters as one complete EPUB or UTF-8 TXT attachment. Unlike `/export`, this returns the file directly, not a chapter-range ZIP. |

## Import local books

Open **Books → Upload your books**, choose one or more `.epub`, `.txt`, or `.zip`
files, then select **Upload to library**. Open an imported book and choose
**Download EPUB** or **Download TXT**; either input format can be downloaded in
either output format.

- A ZIP can contain multiple stories in nested folders. Each EPUB/TXT file is
  imported independently; nested ZIPs and other formats are skipped.
- EPUB imports retain title, author, language, synopsis and reading order.
  Text and basic formatting are imported; images and active/external resources
  are omitted. TXT accepts UTF-8 (with or without BOM) and BOM-marked UTF-16.
  Recognized Vietnamese, English or Chinese chapter headings split the book;
  text without headings is stored as one chapter.
- Imported books use the same on-disk Library storage as crawled books and remain
  available after restarting. Duplicate names create separate books, never
  overwrite existing content. Local books have no website source to re-crawl.
- The form reports imported books, individual failures and skipped files.
  Retrying a partially successful ZIP creates additional copies of its valid
  stories; choose only failed stories to avoid duplicates.
- Limits: 50 MiB combined uploaded files, 200 MiB expanded content and 10,000
  archive entries per request, including EPUB internals. Unsafe paths, links,
  encrypted entries and excessive compression ratios are rejected.

```bash
curl -F 'files=@novel.epub' -F 'files=@stories.zip' \
  http://127.0.0.1:8000/api/books/upload
curl -OJ 'http://127.0.0.1:8000/api/books/BOOK_ID/download?format=txt'
```


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

On qbmfxs.com, chapters can span linked `/2`, `/3`, ... pages; the crawler
combines their visible text and encoded `p_key` remainder before saving. Books
downloaded before this fix may contain truncated chapters: on a book's detail
page, choose **Re-crawl & overwrite** next to **Fetch missing** to replace all
saved chapters. The equivalent API call is `/api/extract` with `"overwrite": true`.

DocLN (`docln.net`, `ln.hako.vn`) chapter pages may contain a shuffled, XOR-encoded
body instead of visible paragraphs. The Hako crawler decodes it before saving
the chapter, including illustrations. If an older download contains only a
hidden chapter heading, use **Re-crawl & overwrite** to replace the saved body.

For these two hosts, the Hako scraper uses Cloudflare DNS-over-HTTPS because some
local DNS providers resolve them to loopback (`127.0.0.1`/`::1`), causing
`transport failure (ConnectionError)` before a page can load. Requests still go
through the normal scraper with the original host and TLS verification; no
system DNS change or pinned site IP is needed.

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

Before translation, a deterministic RAW scan filters generic words and sentence fragments, then finds likely names and stable terms. Gemini returns one of three outcomes: confirmed, rejected, or still unresolved. Confirmed entries merge into the locked dictionary; rejected sources enter a small cache; unresolved candidates carry chapter-labelled sentence evidence forward in `dictionary.json`. A candidate is retried only when new evidence or occurrences arrive, its type becomes clearer, or a previous provider/schema failure needs a later retry. The resolver sends only locked and relevant candidate evidence; unresolved terms never enter translation prompts. An ordinary chapter fits in one translation request. Long chapters use paragraph and estimated output-token budgets, with stable paragraph IDs, previous accepted Vietnamese context, and one RAW lookahead paragraph. Local validation checks IDs, numbers, Chinese residue, locked terms, and truncation before an atomic chapter commit. Only suspicious content receives targeted AI QA; confirmed defects get one complete-paragraph repair.

Completed batches include translated.txt and translated.json for Vietnamese chapters, dictionary.json for the full merged cumulative book dictionary, and unresolved.json for candidates needing review.

The exported `dictionary.json` is a versioned object containing locked `entries`, carry-forward `unresolved` records, and a `rejected` cache. The same file can be supplied to the next batch. The rejected cache is not a glossary and is never sent to translation; it prevents already rejected sources from consuming resolver requests again. `unresolved.json` contains only the live evidence queue. Each queue record stores `source`, `possible_type`, `evidence` objects with `chapter` and full-sentence `text`, first/last chapter, occurrence count, resolver attempts, and an optional `last_error`. Evidence is deduplicated and capped at five samples per candidate. Flat entry arrays remain accepted as inputs. Each locked entry has exactly source, translation, type, gender, status=locked, and aliases; aliases have source and translation. Canonical entry types are character, location, institution, book_title, memorial, book_section, and term. The candidate resolver uses character, location, institution, book_title, and term. Older organization, ability, item, and concept types migrate explicitly to institution or term; a v10 dictionary cannot contain legacy entry types. Gender is male, female, unknown, or not_applicable (required for non-person entries). Versioned older dictionaries remain accepted as input and produce a migration record. Existing noisy unresolved records are migrated through the same deterministic filter before they can reach the resolver.

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
  "unresolved": [],
  "rejected": []
}
~~~

An unresolved record looks like `{"source":"何心隐","possible_type":"character","evidence":[{"chapter":147,"text":"何心隐在书院讲学。"}],"first_seen_chapter":147,"last_seen_chapter":147,"occurrences":1,"resolve_attempts":1}`. Confirmed entries move to `entries`; rejected items move to the non-glossary `rejected` cache.

The API exposes POST /api/translation/jobs with {raw, dictionary?}, saved-job list/detail/resume/cancel/delete routes, and output downloads under /api/translation/jobs/{id}/outputs/. Parser errors include input, error_type, chapter, line, previous_line, and reason. Request logs record requested and actual models, key slots, finish reasons, retries, token counts when available, and QA outcomes. API keys are never written to checkpoints.

In Books → a saved chapter, import a `.json` dictionary once per book and choose **Dịch chương**. The reader keeps the original HTML for **RAW** and stores Vietnamese title/paragraphs separately for **Dịch**; opening a completed chapter again does not repeat the provider request. The validated job's cumulative dictionary (including newly confirmed terminology) becomes the input for the next chapter, so only one chapter per book translates at a time. Replacing the imported file replaces the cumulative dictionary; translations affected by a changed RAW body or relevant locked terms are marked stale and can be translated again. Failed/interrupted jobs expose the existing resume, cancel, and manual-review actions inside the reader. The separate Translate tab still accepts standalone RAW/dictionary batches unchanged.
When saved HTML repeats the current chapter heading inside its body, the reader adapter uses that original heading as the translation source title and removes the duplicate line from the generated RAW (keeping prose appended to it). This preserves the Chinese title even if TOC title translation already replaced the chapter's saved title with `Chương N: …`. If the saved title says `Chương 58` but its library ID is 59, a matching heading at the start of the body is likewise removed; translation still uses library chapter ID 59. Other mismatched headings remain validation errors. Without an in-body source heading, the matching Vietnamese chapter-number wrapper is stripped before translation. Standalone RAW batch uploads still reject duplicate and backwards chapter headings.
If Gemini returns `PROHIBITED_CONTENT` during a paragraph repair, rotating API keys does not resolve that provider refusal. The job remains failed with its pending translation intact; use the reader's manual-review panel to compare RAW, the current Vietnamese text, and the listed defect. Replace the complete paragraph with a validated correction, or explicitly accept the current text as an audited override, then resume. Older blocked jobs also recover this panel from their request logs. Cancel the job instead if no manual decision is appropriate; neither path bypasses Gemini's block by resending the same material to another model.

`TRANSLATION_AUTHOR_NOTE_POLICY` controls clearly marked author/platform notes: `preserve` (default), `remove` from reader output, or `separate` into `author-notes.txt`. All source IDs remain in the internal chapter artifact. Request-budget warnings and a hard guard stop runaway jobs; per-operation counts appear in the browser and batch result.

See [the translation pipeline design](docs/translation-pipeline.md) for details.
