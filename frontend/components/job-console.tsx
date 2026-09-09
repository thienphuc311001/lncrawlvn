'use client';

import { useEffect, useRef, useState } from 'react';

import { useJobRunner } from './job-runner';

const API_BASE = 'http://127.0.0.1:8000';
const POLL_INTERVAL_MS = 800;
/** Terminal panel holds a bounded history so huge novels cannot flood the DOM. */
const MAX_RENDERED_LINES = 300;
/** Cap kept on the raw accumulated buffer (the DOM only renders the tail). */
const MAX_BUFFER_LINES = 4000;
const MAX_FAILED_RECAP = 20;

type LogLine = {
  text: string;
  level: 'info' | 'warning' | 'error';
  style?: React.CSSProperties;
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
  saved_count?: number;
  error: string | null;
  first_log_index?: number;
  logs: { t: number; level: string; message: string }[];
  chapters: JobChapter[];
};

function toLine(log: { t: number; level: string; message: string }): LogLine {
  const level = log.level as LogLine['level'];
  return { text: `[${log.t.toFixed(1)}s] ${log.message}`, level };
}

function summaryLines(job: Job): LogLine[] {
  const lines: LogLine[] = [
    {
      text: `→ ${job.status} · source=${job.source || '?'} · requested=${job.requested} · found=${job.total_chapters} · ok=${job.success_count} · failed=${job.failed_count}`,
      level: job.status === 'failed' || job.failed_count > 0 ? 'warning' : 'info',
      style: { marginTop: '12px' },
    },
  ];
  if (job.error) lines.push({ text: `✗ ${job.error}`, level: 'error' });
  return lines;
}

/** Recaps failed chapters from the structured per-chapter results, so
 * failures stay visible even when their log lines were evicted. */
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


export default function JobConsole() {
  const { job, jobKey, clearJob, setJobStatus } = useJobRunner();
  const [open, setOpen] = useState(true);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'failed'>('idle');
  const [lineCount, setLineCount] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [copied, setCopied] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // Reset everything when a different job is tracked.
  useEffect(() => {
    setLines([]);
    setStatus(job ? 'running' : 'idle');
    setJobStatus(job ? 'running' : 'idle');
    setLineCount(0);
    setElapsed(0);
    setOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.jobId, jobKey]);

  // Live run clock while the job is active.
  useEffect(() => {
    if (status !== 'running') return;
    const timer = setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [status]);

  // Keep the newest lines visible while streaming.
  useEffect(() => {
    const box = boxRef.current;
    if (box && open) box.scrollTop = box.scrollHeight;
  }, [lines, open]);

  useEffect(() => {
    if (!job) return;
    let cancelled = false;
    /** Absolute index (first_log_index + array position) of the next log to render. */
    let renderedAbsolute = -1; // -1 → first snapshot renders only the recent tail

    const run = async () => {
      try {
        const load = async (): Promise<Job> => {
          const res = await fetch(`${API_BASE}/api/jobs/${job.jobId}`);
          if (!res.ok) throw new Error(`API returned ${res.status}`);
          return res.json();
        };
        let jobData = await load();
        while (!cancelled) {
          const first = jobData.first_log_index ?? 0;
          const lastAbs = jobData.logs.length + first;
          const start =
            renderedAbsolute < 0
              ? Math.max(first, lastAbs - 150) // trim long-standing history on attach
              : Math.min(renderedAbsolute, lastAbs); // guard against log eviction
          const fresh: LogLine[] = [];
          for (let abs = start; abs < lastAbs; abs += 1) {
            const line = toLine(jobData.logs[abs - first]);
            if (abs === first) line.style = { marginTop: '12px' };
            fresh.push(line);
          }
          if (fresh.length) {
            setLines((prev) => {
              const next = [...prev, ...fresh];
              return next.length > MAX_BUFFER_LINES
                ? next.slice(next.length - MAX_BUFFER_LINES)
                : next;
            });
          }
          renderedAbsolute = lastAbs;
          setLineCount(lastAbs);

          if (jobData.status === 'done' || jobData.status === 'failed') {
            setStatus(jobData.status);
            setJobStatus(jobData.status);
            setLines((prev) => [...prev, ...summaryLines(jobData), ...failedChapterLines(jobData)]);
            break;
          }
          setStatus('running');
          setJobStatus('running');
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
          if (cancelled) break;
          jobData = await load();
        }
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setStatus('failed');
        setJobStatus('failed');
        setLines((prev) => [
          ...prev,
          { text: `✗ ${message} — is the backend running? (lnmini run dev)`, level: 'error' },
        ]);
      }
    };

    void run();

    return () => {
      cancelled = true;
    };
  }, [job?.jobId, jobKey]);

  if (!job) return null;

  const statusLabel: Record<string, string> = {
    idle: 'IDLE',
    running: 'RUNNING',
    done: 'DONE',
    failed: 'FAILED',
  };
  const visible = lines.slice(-MAX_RENDERED_LINES);

  const copyLog = async () => {
    try {
      await navigator.clipboard.writeText(lines.map((line) => line.text).join('\n'));
      setCopied(true);
    } catch {
      setCopied(false);
    }
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <>
      {status === 'running' && !open && (
        <button
          type="button"
          className="job-console-fab"
          onClick={() => setOpen(true)}
          aria-label="Mở job console"
          title="Job đang chạy — bấm để mở console"
        >
          <span className={`jc-dot ${status}`} aria-hidden="true" />
          ▤
        </button>
      )}
      <aside className={`job-console${open ? ' open' : ''}`} aria-label="Job console">
        <header className="job-console-head">
          <button
            type="button"
            className="job-console-title"
            onClick={() => setOpen((value) => !value)}
            title={open ? 'Thu gọn' : 'Mở rộng'}
          >
            <span className={`jc-dot ${status}`} aria-hidden="true" />
            <span className={`jc-status ${status}`}>{statusLabel[status]}</span>
            <span className="jc-name">{job.label}</span>
            <span className="jc-meta">
              {lineCount} dòng{elapsed > 0 ? ` · ${elapsed}s` : ''}
            </span>
          </button>
          <div className="job-console-actions">
            <button type="button" className="btn btn-ghost" onClick={() => void copyLog()}>
              {copied ? '✓ Đã copy' : '⧉ Copy'}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setOpen((value) => !value)}
              aria-label={open ? 'Thu gọn console' : 'Mở rộng console'}
            >
              {open ? '▾' : '▴'}
            </button>
            <button type="button" className="btn btn-ghost" onClick={clearJob} aria-label="Đóng console">
              ✕
            </button>
          </div>
        </header>
        {open && (
          <div className="job-console-body" role="log" ref={boxRef}>
            {visible.map((line, index) => (
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
            {status === 'running' && (
              <span className="line">
                <span className="job-cursor" aria-hidden="true" />
              </span>
            )}
          </div>
        )}
      </aside>
    </>
  );
}
