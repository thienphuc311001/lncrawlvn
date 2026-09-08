'use client';

import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

type LogLine = {
  text: string;
  level: 'info' | 'warning' | 'error';
  style?: CSSProperties;
};

type JobChapter = {
  id: number;
  title: string;
  url: string;
  success: boolean;
  error: string | null;
};

type Job = {
  job_id: string;
  url: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  source: string;
  title: string;
  author: string;
  total_chapters: number;
  requested: string;
  success_count: number;
  failed_count: number;
  error: string | null;
  logs: { t: number; level: string; message: string }[];
  chapters: JobChapter[];
};

const API_BASE = 'http://127.0.0.1:8000';
const POLL_INTERVAL_MS = 800;
/** Terminal panels hold a bounded history so huge novels cannot flood the DOM. */
const MAX_RENDERED_LINES = 300;

const IDLE_LINES: LogLine[] = [
  { text: 'lncrawl-mini ready. Paste a novel URL and press Extract.', level: 'info' },
];

type DemoOutputProps = {
  /** URL typed into the hero input (empty until the user enters one). */
  url: string;
  /** Incremented every time a crawl should (re)start. */
  runToken: number;
  /** Notifies the parent when a crawl starts / finishes. */
  onRunningChange: (running: boolean) => void;
};

function jobLine(job: Job, index: number): LogLine {
  const prefix = `[${job.logs[index].t.toFixed(1)}s]`;
  return { text: `${prefix} ${job.logs[index].message}`, level: job.logs[index].level as LogLine['level'] };
}

function summaryLines(job: Job): LogLine[] {
  const lines: LogLine[] = [
    {
      text: `→ ${job.status} · source=${job.source || '?'} · requested=${job.requested} · found=${job.total_chapters} · ok=${job.success_count} · failed=${job.failed_count}`,
      level: job.status === 'failed' || job.failed_count > 0 ? 'warning' : 'info',
      style: { marginTop: '12px' },
    },
  ];
  if (job.error) {
    lines.push({ text: `✗ ${job.error}`, level: 'error' });
  }
  return lines;
}

/** Recaps failed chapters from the structured per-chapter results, so
 * failures stay visible even when their log lines were evicted. */
const MAX_FAILED_RECAP = 20;

function failedChapterLines(job: Job): LogLine[] {
  const failed = job.chapters.filter((chapter) => !chapter.success);
  if (failed.length === 0) return [];
  const lines: LogLine[] = [
    {
      text: `⚠ Failed chapters (${failed.length}):`,
      level: 'warning',
      style: { marginTop: '12px' },
    },
  ];
  for (const chapter of failed.slice(0, MAX_FAILED_RECAP)) {
    const reason = chapter.error ? ` — ${chapter.error.slice(0, 140)}` : '';
    lines.push({ text: `  ✗ Ch ${chapter.id} · ${chapter.title}${reason}`, level: 'error' });
  }
  if (failed.length > MAX_FAILED_RECAP) {
    lines.push({ text: `  … and ${failed.length - MAX_FAILED_RECAP} more`, level: 'warning' });
  }
  return lines;
}

export default function DemoOutput({ url, runToken, onRunningChange }: DemoOutputProps) {
  const [lines, setLines] = useState<LogLine[]>(IDLE_LINES);
  const [running, setRunning] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (runToken === 0) return;

    let cancelled = false;
    const targetUrl = url.trim();

    const run = async () => {
      setRunning(true);
      onRunningChange(true);
      setLines([{ text: `$ lncrawl extract --url ${targetUrl || 'https://...'}`, level: 'info' }]);

      if (!targetUrl) {
        setLines((prev) => [...prev, { text: '✗ No URL provided', level: 'error' }]);
        setRunning(false);
        onRunningChange(false);
        return;
      }

      try {
        const res = await fetch(`${API_BASE}/api/extract`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: targetUrl }),
        });
        if (!res.ok && res.status !== 202) {
          const detail = await res.json().catch(() => null);
          throw new Error(detail?.detail || `API returned ${res.status}`);
        }
        let job: Job = await res.json();

        let renderedLogs = 0;
        while (!cancelled) {
          const fresh: LogLine[] = [];
          for (let i = renderedLogs; i < job.logs.length; i += 1) {
            const line = jobLine(job, i);
            if (i === 0) line.style = { marginTop: '12px' };
            fresh.push(line);
          }
          renderedLogs = job.logs.length;
          if (fresh.length) setLines((prev) => [...prev, ...fresh]);

          if (job.status === 'done' || job.status === 'failed') {
            setLines((prev) => [...prev, ...summaryLines(job), ...failedChapterLines(job)]);
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
          const poll = await fetch(`${API_BASE}/api/jobs/${job.job_id}`);
          if (!poll.ok) throw new Error(`Job poll returned ${poll.status}`);
          job = await poll.json();
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setLines((prev) => [
          ...prev,
          {
            text: `✗ ${message} — is the backend running? (lnmini run dev)`,
            level: 'error',
          },
        ]);
      } finally {
        if (!cancelled) {
          setRunning(false);
          onRunningChange(false);
        }
      }
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

  // Keep the newest lines visible as logs stream in.
  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const rendered = lines.slice(-MAX_RENDERED_LINES);

  return (
    <div className="demo-output" role="log" aria-label="Crawl output" ref={boxRef}>
      {rendered.map((line, index) => (
        <span
          key={index}
          className={`line line-in${line.level === 'error' ? ' error' : ''}${
            line.level === 'warning' ? ' warning' : ''
          }`}
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
