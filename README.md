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
| `/api/books/{book_id}/export?format=epub\|txt` | GET | Build an EPUB/TXT and download it as a ZIP — one `<title>_0001-0100.epub`-style file per 100-chapter folder |


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

You can also run the same resumable pipeline directly from files:

```sh
./scripts/translate.sh --raw '/path/to/raw.txt' --vietphrase '/path/to/vietphrase.txt' --output translation-output
```

Add `--dictionary /path/to/dictionary.json` for later batches. Use `--check-inputs` to
validate all chapter pairs without API calls, or `--first 1` for a live first-chapter
test. Omit `--first` to translate the entire batch. The command loads the project-root
`.env`, prints actual task/model progress, and creates only `translated.json` and
`dictionary.json` in the output directory after final validation. Re-run the same
command to resume checkpoints; Ctrl+C cancels cleanly. Existing different output files
are protected: choose a fresh output directory for a different batch.
CLI batches also appear in the browser's saved batches, where they can be cancelled.
The browser and CLI share checkpoints and prevent concurrent execution of the same batch.

The **Translate** tab runs a separate, resumable Gemini pipeline; extraction and the book
library remain available. Start the existing API/frontend as usual after `uv sync`.
Configure credentials in the project-root `.env` file **on the API server only**.
The backend loads it automatically at startup; existing environment variables take precedence.
Copy `.env.example` to `.env` if needed, then fill in your key:

```dotenv
GOOGLE_AI_API_KEY=your Google AI Studio key
GOOGLE_AI_API_KEY_BACKUP=your optional backup Google AI Studio key
GOOGLE_AI_API_KEY_THIRD=your optional third Google AI Studio key
# More keys, if needed: GOOGLE_AI_API_KEYS=key3,key4
TRANSLATION_WORKERS=2
# Address/title register: sino-vietnamese (default for Chinese novels), modern, or auto
TRANSLATION_DICTIONARY_REGISTER=sino-vietnamese
```

Restart the backend after editing `.env`. This file is ignored by Git; keep it in the
project root, separate from the frontend directory. Workers are optional (1–3, default 2).

The scheduler rotates fairly across every model/key pair. A daily quota error disables
only that pair for the current process run. RPM and TPM quota errors suspend only that
pair for 60 seconds while work continues on ready pairs. A 429 without a recognized
quota dimension is logged as `UNKNOWN_ERROR`, never assumed to be a daily limit. Keys
are deduplicated in this order: primary, backup, third, then the comma-separated
additional keys. Logs show only key slot numbers. HTTP 503 uses bounded retries before
the pair is skipped for that operation; authentication/configuration errors disable the
affected key for the current run.
Google applies quota per project, not per key, so keys from the same project share
quota ([Google rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits)).

Upload exactly these inputs:

- **RAW**: UTF-8 Chinese text, the semantic source of truth.
- **VIETPHRASE**: corresponding externally converted UTF-8 text, for readings and names.
- **DICTIONARY** (optional): an earlier `dictionary.json`, carried forward cumulatively.

### TXT export layout (VietPhrase-safe)

TXT exports are chunked for VietPhrase.app, whose internal file translator is
observed to merge adjacent source lines around ~4000 cumulative characters:

- Each chapter is a separator, then the crawled body with its native `第N章`
  heading on its own first line (no `Chapter N:` synthetic wrapper).
- Long chapters are split into deterministic blocks of at most 3000 characters
  (boundary search starts at 2600), separated by exactly one blank line.
- Complete source paragraphs are never split to fill a block; only a single
  paragraph longer than 3000 characters is split internally at Chinese sentence
  punctuation.
- Removing the inserted blank lines reproduces the crawled text exactly, so
  translation pairing is unaffected: blank lines were already block boundaries.

Defaults `VIETPHRASE_SAFE_BLOCK_TARGET=2600` / `VIETPHRASE_SAFE_BLOCK_MAX=3000`
come from `.env.example`, and can be overridden in the settings modal
(`safe_block_target`, `safe_block_max`) or with the CLI flags
`--safe-block-target` / `--safe-block-max`.

### Chapter parsing and pairing

Use numbered headings such as `第十一章`, `Chapter 11`, or `Chương 11`. Any positive
chapter number is accepted for `第N章`, `Chapter N`, `Chương N`, and VietPhrase.app's
rendered word order `Thứ N chương` (`N >= 1`), so a file may begin at an arbitrary chapter
(for example 101, 501, or 1201) and its first detected
number becomes that file's starting chapter. Chapter 1 is never required, headings are
never renumbered, and the same shared parser is used for RAW and VIETPHRASE. A file with
no recognized heading anywhere reports `no_chapter_headings` without blaming line 1.
Numbered
volume headings can scope chapters: chapter numbering may restart in a new volume, and
chapter identity then includes both volume and chapter number. Within each scope, chapter
numbers must increase; gaps are allowed (for example, chapters 3, 5, and 8). RAW and
VIETPHRASE must have the same volume/chapter identities in the same order. Unscoped files
continue to use chapter number alone; a volume-scoped file is not silently paired with an
unscoped one.

Parsing is conservative: export headers generated by this application's text binder are
supported, including the older binder's wrapper form (current TXT exports emit only the
separator and the body's native heading). The exact RAW exporter form `Chapter N: 第N章 <title>` followed by decorative
separators and the identical Chinese heading is one chapter: the wrapper is the boundary
and the embedded duplicate is excluded from translation prose while being preserved in
parser diagnostics. A wrapper truncated inside a parenthetical note is also recognized
when its complete main title matches and its unfinished note is a normalized prefix of
the embedded heading's complete note; arbitrary partial-title matches are not accepted.
The corresponding VietPhrase exporter wrapper and repeated `Thứ N chương` heading are
handled by the same title-matching rule, including truncated parenthetical notes. That
heading form is itself a recognized boundary everywhere else, so a conflicting embedded
title is reported as a duplicate rather than silently merged.
Other clearly redundant duplicate headings and recognizable tables of
contents can be ignored only when they do not discard chapter content. This does **not**
permit merging different chapters that share a number, dropping prose between duplicate
headings, or assuming arbitrary introductory text is a TOC. Ambiguous headings, conflicting
duplicates, and repeats after meaningful prose are rejected instead of silently losing text.

After structural pairing, logical lines and blank block boundaries are aligned locally.
Editor visual wrapping is irrelevant. Limited split/merge recovery requires matching
punctuation anchors inside corresponding blocks; ambiguous structure stops for manual
review without calling Gemini. Paragraph IDs, never character offsets, connect RAW,
VietPhrase, translated text, and repairs.

Only these fixed models are configured, in this order:

1. `gemini-3.1-flash-lite`
2. `gemini-3.5-flash-lite`
3. `gemini-3.7-flash`

Every AI operation prefers the primary model on a ready key. The central scheduler
allows two requests by default, spaces starts by at least 300 ms, uses a 120-second
attempt timeout, and permits two attempts per pair for temporary provider failures.
Daily quota disables only that model/key for the process run. RPM/TPM cooldown lasts
60 seconds for that pair while available pairs continue; if all usable pairs are
cooling down, only the earliest recovery must be awaited. Authentication/configuration
errors disable the affected key. The configured IDs must be available to your AI Studio
account; there is no model discovery or substitution. Credentials are never returned
to the browser or written to checkpoints.

The translation workspace shows a live **Translation activity** log with task names,
model attempts, queued/running/success/failed states, retry waits, fallback switches,
checkpoint reuse and cancellation. It polls every 1.5 seconds while a batch is active
and retains the latest 100 events after stopping. The complete event history is stored
in the job's `events.jsonl`; request diagnostics remain in `requests.jsonl`, and the CLI
prints those request events. If all three models fail, the error reports each model's
failure in the configured order, rather than showing only the last backup error.

The mandatory global dictionary phase scans the **entire input batch locally** before
any translation. Candidates originate in RAW; aligned VietPhrase readings are only
supporting evidence. Generic phrases, malformed spans and uncertain candidates become
`IGNORE` diagnostics and never enter the strict dictionary. Strong local full-name
evidence can become `CONFIRMED`; genuinely plausible ambiguity is sent to one bounded
resolver batch. Resolver review, malformed output and uncertain aliases become IGNORE,
not provisional runtime mappings.

Schema, Unicode, source eligibility, duplicate sources, translations, aliases/forms,
namespace collisions and RAW presence are audited locally. Malformed inherited records
are quarantined; different Chinese identities may share one Vietnamese target. Only
CONFIRMED entries are frozen and enforced. A new uncertain term after freeze is
diagnostic-only and cannot mutate the dictionary.

Address and title forms keep one consistent style. Each confirmed Chinese reference form
is classified as literal kinship, social honorific, official title, rank, role reference,
nickname or alias, and a new form is compared against confirmed forms of the same class.
The batch register is Sino-Vietnamese by default (`TRANSLATION_DICTIONARY_REGISTER`,
also `sino-vietnamese`/`modern`/`auto`), so the resolver never picks a wording only
because it is the most natural modern conversational option. Before freeze, a form that
contradicts the register is rewritten to the established rendering (`唐姐` → `Đường tỷ`,
`邱长官` → `Trưởng quan Khâu`) or dropped with an audit record; the canonical identity is
never changed and a bare address form never becomes its own character. The final dictionary
is frozen with a stable hash **before the first translation**.
All chapters/chunks use this same hash. New uncertain terminology reported later is
written to the ignored-term audit; it never changes the frozen dictionary or finalized work.

Chunks are built locally at paragraph boundaries, normally around 4,000–6,000 RAW
characters. Translation uses only the current RAW/VP window, local chapter metadata,
relevant frozen dictionary entries and the previous two finalized paragraphs (bounded
in size). There is no separate AI chapter analysis. Output checks run locally for
coverage, IDs/order, emptiness, Chinese residue, frozen wording, Unicode, title shape,
length anomalies, literal placeholders and suspicious duplicated paragraphs. A successful
translation causes zero AI review/repair requests. Only actual findings invoke one targeted
repair for exact failed IDs, preserving unaffected text. A persistent failure stops with
concrete findings.
These deterministic checks detect concrete defects; they do not prove full semantic
equivalence or replace human editorial review of subtle narrative meaning.

**Request statistics** show logical operations and physical API attempts separately for
terminology resolution, translation and repair. Retry,
model fallback and cache reuse counts are also visible. Alignment, scanning, occurrence
aggregation, evidence selection, structural audit, chunking and normal validation explicitly
report zero API calls. Every Gemini activity event includes its operation and reason.
See [the architecture and verification report](docs/translation-pipeline.md) for the
previous-operation inventory, exact pipeline order, measurements and behavioral tests.

A completed batch exposes **translated.txt**, **translated.json**, and **dictionary.json**.
`translated.json` contains a `chapters` array of records with `number`, `title`, and `text`.
Volume-scoped records also include `volume`; unscoped records retain the existing shape.
Use `(volume, number)` as the chapter identity when a volume is present, not `number` alone:

```json
{
  "chapters": [
    {"volume": 1, "number": 1, "title": "Khởi đầu", "text": "Nội dung chương…"},
    {"volume": 2, "number": 1, "title": "Hành trình mới", "text": "Nội dung chương…"}
  ]
}
```

The dictionary format is version 4. `entries` contains confirmed mappings only;
ignored candidates are stored separately in audit/report checkpoints. A durable
`dictionary-resolution-report.json` checkpoint and the job snapshot expose counts and
reasons without turning diagnostics into enforcement rules. `form_kinds` and
`address_register` record the class and register enforced for the confirmed address
forms, and the top-level `register` key makes re-freezing a checkpoint a fixed point.

```json
{
  "version": 4,
  "entries": [
    {
      "source": "唐菲菲",
      "translation": "Đường Phỉ Phỉ",
      "type": "character",
      "status": "locked",
      "gender": "unknown",
      "aliases": [],
      "forms": {"唐姐": "Đường tỷ", "邱科长": "Khoa trưởng Khâu"},
      "form_kinds": {"唐姐": "social_honorific", "邱科长": "official_title"},
      "address_register": "sino-vietnamese",
      "evidence": "RAW evidence supporting this mapping",
      "runtime_state": "CONFIRMED"
    }
  ],
  "statistics": {"total_terms": 1, "total_characters": 1, "total_locations": 0},
  "register": "sino-vietnamese"
}
```

Checkpoints, ignored legacy records, per-operation model/retry diagnostics, original
inputs and temporary dictionaries stay under `$LNCRAWL_DATA_PATH/translations/` (default
`~/.lncrawl-mini/translations/`). They contain novel text and should be stored on a private
volume. Writes are atomic. Identical inputs, pipeline version and model configuration
select the same batch. Completed units are cached by task identity, prompt, input and response schema.
Cancel stops queued/in-flight requests and preserves completed checkpoints. Select a saved
batch and choose **Resume batch** after quota recovery or server restart. A failed chapter
never deletes completed chunks from other chapters. Failed validation does not expose
unvalidated final artifacts.

**Pipeline upgrade:** compatible version-5 jobs that stopped during dictionary preparation
can resume with their original inputs. Confirmed mappings and valid completed chunks are
reused; old provisional/report-only/fallback records migrate to IGNORE. Frozen dictionaries
from incompatible terminology schemas are reduced to confirmed entries before translation,
and contradictory confirmed mappings fail clearly rather than being guessed. Cleaning an
address/title form against the batch register rewrites or drops only that form, updates the
dictionary hash and revalidates completed chapters through the existing compatible-hash
path, so unaffected translation work is reused.

Translation API routes:

```text
GET  /api/translation/config
POST /api/translation/jobs                 {raw, vietphrase, dictionary?}
GET  /api/translation/jobs
GET  /api/translation/jobs/{id}
GET  /api/translation/jobs/{id}/dictionary-report
POST /api/translation/jobs/{id}/cancel
POST /api/translation/jobs/{id}/resume
GET  /api/translation/jobs/{id}/outputs/translated.json
GET  /api/translation/jobs/{id}/outputs/translated.txt
GET  /api/translation/jobs/{id}/outputs/dictionary.json
```

Structural input failures return HTTP `422` with a structured FastAPI `detail` object.
Its fields are `error`, `severity`, `input`, `line`, `heading`, `chapter`,
`previous_chapter`, `reason`, and `message`; `volume`, `previous_line`, and `counterpart`
may add context. Unavailable context can be `null`. Use the reported input, source line,
and heading to locate the problem rather than renumbering or dropping text blindly.
The response envelope is `{ "detail": { ... } }`, not a JSON-encoded string.

The frontend renders these fields individually, including optional counterpart information.
Other failures can still use a string `detail`, and ordinary FastAPI/Pydantic request
validation can return a `detail` array; both remain supported. A job that fails structural
validation in the background can expose the same object as `error_detail` alongside its
human-readable `error` string. Progress `active_chapters` keys remain `"N"` for unscoped
chapters and use `"vV-cN"` for scoped ones (for example `"v2-c3"`); the UI displays
**Volume 2 / Chapter 3**.

Use **one ASGI process**: the scheduler and active-job ownership are process-wide. A
multi-process/multi-host deployment requires a shared queue/lease and quota coordinator.
Like the existing extraction API, these routes are designed for trusted local use; put
authentication and request-size limits in front of any network deployment. Input limits
are 20 million RAW characters and 40 million VietPhrase characters per batch. Validators
are model-assisted: automated checks do not prove literary/semantic accuracy, so review
representative outputs before committing a dictionary to later books/batches.

Offline verification (no key or paid requests needed):

```sh
uv run python -m unittest discover -s tests -v
cd frontend
bun run build
```
