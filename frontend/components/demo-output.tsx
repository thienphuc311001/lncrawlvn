'use client';

import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';

type DemoLine = {
  text: string;
  success?: boolean;
  style?: CSSProperties;
  animate?: boolean;
};

type DemoOutputProps = {
  /** URL typed into the hero input (empty until the user enters one). */
  url: string;
  /** Incremented every time a crawl simulation should (re)start. */
  runToken: number;
  /** Notifies the parent when the simulation starts / finishes. */
  onRunningChange: (running: boolean) => void;
};

/** The static log shown on first paint — mirrors the index.html mockup exactly. */
const STATIC_LINES: DemoLine[] = [
  { text: '$ novelcrawler extract --url https://...' },
  { text: '→ Detecting novel structure...', style: { marginTop: '12px' } },
  { text: '✓ Found 247 chapters', success: true },
  { text: '→ Parsing chapter metadata...' },
  { text: '✓ Extracted title, author, summary', success: true },
  { text: '→ Downloading chapter content...' },
  { text: '✓ 247/247 chapters complete', success: true },
  { text: '→ Building EPUB...' },
  { text: '✓ novel-title.epub (2.4 MB)', success: true, style: { marginTop: '12px' } },
];

const FALLBACK_CHAPTERS = 247;

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  }
  return hash;
}

/** Derive a plausible chapter count from the URL (falls back to the mockup's 247). */
function deriveChapters(url: string): number {
  if (!url) return FALLBACK_CHAPTERS;
  return 96 + (hashString(url) % 304);
}

/** URL path segments that name a chapter rather than the novel itself. */
const CHAPTER_SEGMENTS = /^(chapter|chap|chuong)[-_]?\d*$/i;
/** Generic URL path segments that never identify a specific novel. */
const GENERIC_SEGMENTS =
  /^(novels?|books?|read|reads|story|stories|series|fiction|webnovel|light-?novel)$/i;

/** Derive a filename from the URL path, skipping chapter-like, generic, and numeric segments. */
function deriveTitle(url: string): string {
  if (!url) return 'novel-title';
  try {
    const { pathname } = new URL(url);
    const segments = pathname.split('/').filter((segment) => segment.length > 0);
    const meaningful = segments.filter(
      (segment) =>
        !CHAPTER_SEGMENTS.test(segment) &&
        !GENERIC_SEGMENTS.test(segment) &&
        !/^\d+$/.test(segment)
    );
    const last = meaningful[meaningful.length - 1];
    if (last) {
      return last.replace(/\.(html?|php|aspx?)$/i, '');
    }
  } catch {
    // Not a parseable URL — fall through to the mockup default.
  }
  return 'novel-title';
}

/** Scale the EPUB size with the chapter count (247 chapters → 2.4 MB, as in the mockup). */
function deriveSizeMb(chapters: number): string {
  return (chapters * 0.0097 + 0.05).toFixed(1);
}

export default function DemoOutput({ url, runToken, onRunningChange }: DemoOutputProps) {
  const [lines, setLines] = useState<DemoLine[]>(STATIC_LINES);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (runToken === 0) return;

    let cancelled = false;
    const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
    const push = (line: DemoLine) => setLines((prev) => [...prev, { ...line, animate: true }]);
    const replaceLast = (text: string) =>
      setLines((prev) =>
        prev.map((line, index) => (index === prev.length - 1 ? { ...line, text } : line))
      );

    const chapters = deriveChapters(url);
    const title = deriveTitle(url);
    const echoedUrl = url || 'https://...';

    const run = async () => {
      setRunning(true);
      onRunningChange(true);
      setLines([{ text: `$ novelcrawler extract --url ${echoedUrl}`, animate: true }]);

      await sleep(450);
      if (cancelled) return;
      push({ text: '→ Detecting novel structure...', style: { marginTop: '12px' } });

      await sleep(750);
      if (cancelled) return;
      push({ text: `✓ Found ${chapters} chapters`, success: true });

      await sleep(420);
      if (cancelled) return;
      push({ text: '→ Parsing chapter metadata...' });

      await sleep(650);
      if (cancelled) return;
      push({ text: '✓ Extracted title, author, summary', success: true });

      await sleep(420);
      if (cancelled) return;
      push({ text: '→ Downloading chapter content...' });

      await sleep(450);
      if (cancelled) return;
      push({ text: `✓ 0/${chapters} chapters complete`, success: true });
      const steps = 22;
      for (let step = 1; step <= steps; step += 1) {
        if (cancelled) return;
        const count = Math.min(chapters, Math.round((chapters * step) / steps));
        replaceLast(`✓ ${count}/${chapters} chapters complete`);
        await sleep(70);
      }

      await sleep(350);
      if (cancelled) return;
      push({ text: '→ Building EPUB...' });

      await sleep(850);
      if (cancelled) return;
      push({
        text: `✓ ${title}.epub (${deriveSizeMb(chapters)} MB)`,
        success: true,
        style: { marginTop: '12px' },
      });

      setRunning(false);
      onRunningChange(false);
    };

    void run();

    return () => {
      cancelled = true;
      setRunning(false);
      onRunningChange(false);
    };
    // Re-run only when a new run is requested; `url` is captured at that moment on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runToken]);

  return (
    <div className="demo-output" role="log" aria-label="Crawl demo output">
      {lines.map((line, index) => (
        <span
          key={index}
          className={`line${line.success ? ' success' : ''}${line.animate ? ' line-in' : ''}`}
          style={line.style}
        >
          {line.text}
        </span>
      ))}
      {running && (
        <span className="line">
          <span className="demo-cursor" aria-hidden="true" />
        </span>
      )}
    </div>
  );
}
