# lncrawl-mini · frontend

Next.js frontend for **lncrawl-mini**. Clicking **Extract novel** (or pressing
**Enter** in the URL field) starts a real crawl job on the backend; the global
job console docked at the bottom of the screen streams its live log from
anywhere in the UI (Extract, Books, book detail — tab switches never lose it).

## Requirements

- [Bun](https://bun.sh) 1.4+

## Usage

```bash
bun install      # install dependencies
bun run dev      # dev server on http://localhost:3000
bun run build    # production build
bun run start    # serve the production build
```

## Structure

```
frontend/
├── app/
│   ├── globals.css    # design system ported verbatim from index.html (+ demo additions)
│   ├── layout.tsx     # root layout & metadata
│   └── page.tsx       # landing page composition
└── components/
    ├── top-nav.tsx      # sticky navigation
    ├── hero.tsx         # client: URL input + extract controls
    ├── job-runner.tsx   # client: global job context (start/track jobs app-wide)
    ├── job-console.tsx  # client: global dock streaming live job logs
    ├── features.tsx     # feature grid
    ├── cta-strip.tsx    # call-to-action band
    └── page-foot.tsx    # footer
```

Note: this frontend is presentational only. The actual crawler is the Python CLI
in the repository root: `uv run python -m lncrawl <url>`.
