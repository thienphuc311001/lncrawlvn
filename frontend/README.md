# lncrawl-mini · frontend

Next.js frontend for **lncrawl-mini**. Clicking **Extract novel** (or pressing
**Enter** in the URL field) starts a real crawl job on the backend; the global
job console docked at the bottom of the screen streams its live log from
anywhere in the UI (Extract, Books, book detail — tab switches never lose it).

Đang crawl có nút **Dừng crawl** trong console và ở trang chi tiết sách (kể cả khi
đã đóng console hoặc tải lại trang). Đóng console chỉ ẩn tiến trình, không dừng
crawl. Sau khi yêu cầu dừng, chờ trạng thái **Đã hủy** rồi mới xóa sách; các
chương đã lưu được giữ nguyên.

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
│   ├── globals.css    # shared workspace tokens, components, and responsive styles
│   ├── layout.tsx     # root layout & metadata
│   └── page.tsx       # landing page composition
└── components/
    ├── top-nav.tsx      # sticky navigation
    ├── hero.tsx         # client: extraction form, recent books, and settings
    ├── job-runner.tsx   # client: global job context (start/track jobs app-wide)
    ├── job-console.tsx  # client: global dock streaming live job logs
    ├── settings-modal.tsx # client: crawl tweaks + engine settings (GET/POST /api/config)
    ├── features.tsx     # feature grid
    ├── cta-strip.tsx    # call-to-action band
    └── page-foot.tsx    # footer
```

The frontend uses the FastAPI crawl service. The CLI also remains available:
`uv run python -m lncrawl <url>`.

## Interface checks

```bash
bun run build
bun x tsc --noEmit
bun test tests/translation-workspace.test.tsx
```

The workspace uses the existing React/Next.js stack and system fonts. Books can
be searched by title or author and sorted by date or title. Recent books open
directly from Extract. Settings, export options, and the chapter reader support
keyboard focus trapping, Escape dismissal, and focus restoration.
