# lncrawl-mini · frontend

Next.js landing page for **lncrawl-mini**, ported from the static mockup at
[`../index.html`](../index.html). Clicking **Extract novel** (or pressing **Enter**
in the URL field) plays an animated crawl simulation in the terminal panel —
no backend calls are made.

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
    ├── demo-output.tsx  # client: animated terminal crawl simulation
    ├── features.tsx     # feature grid
    ├── cta-strip.tsx    # call-to-action band
    └── page-foot.tsx    # footer
```

Note: this frontend is presentational only. The actual crawler is the Python CLI
in the repository root: `uv run python -m lncrawl <url>`.
