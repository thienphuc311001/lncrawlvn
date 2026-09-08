'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

const API_BASE = 'http://127.0.0.1:8000';
const POLL_INTERVAL_MS = 800;
/** Mirrors CHAPTERS_PER_FOLDER in lncrawl/library.py */
const FOLDER_SIZE = 100;

type TocChapter = { id: number; title: string; url: string; saved: boolean };

type Book = {
  book_id: string;
  title: string;
  author: string;
  cover_url: string;
  total_chapters: number;
  saved_count: number;
  synopsis: string;
  tags: string[];
  chapters: TocChapter[];
};

type ChapterContent = { id: number; title: string; url: string; body: string };

type ReaderState = { loading: boolean; chapter: ChapterContent | null; error: string };

type FetchJobState = { jobId: string; done: number; total: number; status: string; error: string };

function folderRange(chapterId: number): { start: number; end: number } {
  const start = Math.floor((chapterId - 1) / FOLDER_SIZE) * FOLDER_SIZE + 1;
  return { start, end: start + FOLDER_SIZE - 1 };
}

export default function BookDetail({ bookId, onBack }: { bookId: string; onBack: () => void }) {
  const [book, setBook] = useState<Book | null>(null);
  const [error, setError] = useState('');
  const [openFolders, setOpenFolders] = useState<Set<number>>(() => new Set([1]));
  const [reader, setReader] = useState<ReaderState>({ loading: false, chapter: null, error: '' });
  const [fetchJob, setFetchJob] = useState<FetchJobState | null>(null);
  const [exportError, setExportError] = useState('');
  const [exporting, setExporting] = useState<'epub' | 'txt' | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/books/${encodeURIComponent(bookId)}`);
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      setBook(await res.json());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [bookId]);

  useEffect(() => {
    void load();
  }, [load]);

  /** TOC grouped into consecutive folders of FOLDER_SIZE chapters. */
  const groups = useMemo(() => {
    if (!book) return [];
    const out: { start: number; end: number; chapters: TocChapter[] }[] = [];
    for (const ch of book.chapters) {
      const range = folderRange(ch.id);
      const last = out[out.length - 1];
      if (!last || last.start !== range.start) {
        out.push({ ...range, chapters: [] });
      }
      out[out.length - 1].chapters.push(ch);
    }
    return out;
  }, [book]);

  const missingCount = book ? book.total_chapters - book.saved_count : 0;

  const openChapter = useCallback(
    async (chapterId: number) => {
      setReader({ loading: true, chapter: null, error: '' });
      try {
        const res = await fetch(
          `${API_BASE}/api/books/${encodeURIComponent(bookId)}/chapters/${chapterId}`,
        );
        if (!res.ok) throw new Error(`API returned ${res.status}`);
        setReader({ loading: false, chapter: await res.json(), error: '' });
      } catch (e) {
        setReader({ loading: false, chapter: null, error: e instanceof Error ? e.message : String(e) });
      }
    },
    [bookId],
  );

  const startFetchMissing = useCallback(async () => {
    setExportError('');
    try {
      const res = await fetch(
        `${API_BASE}/api/books/${encodeURIComponent(bookId)}/fetch-missing`,
        { method: 'POST' },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `API returned ${res.status}`);
      setFetchJob({ jobId: data.job_id, done: 0, total: 0, status: data.status, error: '' });

      let job = data;
      while (job.status !== 'done' && job.status !== 'failed') {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        const poll = await fetch(`${API_BASE}/api/jobs/${job.job_id}`);
        if (!poll.ok) throw new Error(`Job poll returned ${poll.status}`);
        job = await poll.json();
        setFetchJob({
          jobId: job.job_id,
          done: job.saved_count,
          total: job.chapters.length,
          status: job.status,
          error: job.error || '',
        });
      }
      setFetchJob((prev) =>
        prev && prev.jobId === job.job_id
          ? { ...prev, status: job.status, error: job.error || '' }
          : prev,
      );
      await load();
      setTimeout(() => setFetchJob((prev) => (prev?.jobId === job.job_id ? null : prev)), 4000);
    } catch (e) {
      setFetchJob(null);
      setExportError(e instanceof Error ? e.message : String(e));
    }
  }, [bookId, load]);

  const exportBook = useCallback(
    async (format: 'epub' | 'txt') => {
      setExporting(format);
      setExportError('');
      try {
        const res = await fetch(
          `${API_BASE}/api/books/${encodeURIComponent(bookId)}/export?format=${format}`,
        );
        if (!res.ok) {
          const detail = await res.json().catch(() => null);
          throw new Error(detail?.detail || `API returned ${res.status}`);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${book?.title || bookId}.${format}.zip`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      } catch (e) {
        setExportError(e instanceof Error ? e.message : String(e));
      } finally {
        setExporting(null);
      }
    },
    [book, bookId],
  );


  if (error && !book) {
    return (
      <section className="section">
        <div className="container">
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            ← Back to library
          </button>
          <p className="muted error-text">✗ {error}</p>
        </div>
      </section>
    );
  }
  if (!book) {
    return (
      <section className="section">
        <div className="container">
          <p className="muted">Loading book…</p>
        </div>
      </section>
    );
  }

  const pct =
    book.total_chapters > 0 ? Math.round((book.saved_count / book.total_chapters) * 100) : 0;
  const reading = reader.chapter;

  return (
    <section className="section">
      <div className="container">
        <button type="button" className="btn btn-ghost" onClick={onBack}>
          ← Back to library
        </button>

        <div className="book-detail-head">
          <div className="book-cover large">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={`${API_BASE}/api/books/${encodeURIComponent(bookId)}/cover`}
              alt=""
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = 'none';
              }}
            />
          </div>
          <div>
            <h1>{book.title}</h1>
            {book.author && <p className="muted">by {book.author}</p>}
            <div className="tag-row">
              {book.tags.slice(0, 6).map((tag) => (
                <span key={tag} className="tag">
                  {tag}
                </span>
              ))}
            </div>
            <p className="book-progress">
              {book.saved_count}/{book.total_chapters} chapters saved · {pct}%
            </p>
            <div className="progress-track wide">
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <div className="detail-actions">
              <button
                type="button"
                className="btn btn-primary"
                disabled={missingCount === 0 || fetchJob !== null}
                onClick={() => void startFetchMissing()}
              >
                {fetchJob ? 'Fetching…' : `Fetch missing (${missingCount})`}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={exporting !== null || book.saved_count === 0}
                onClick={() => void exportBook('epub')}
              >
                {exporting === 'epub' ? 'Packing…' : 'Export EPUB'}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={exporting !== null || book.saved_count === 0}
                onClick={() => void exportBook('txt')}
              >
                {exporting === 'txt' ? 'Packing…' : 'Export TXT'}
              </button>
            </div>
            {exportError && <p className="muted error-text">✗ {exportError}</p>}
            {fetchJob && (
              <p className="muted">
                Fetching missing chapters… {fetchJob.done}
                {fetchJob.total ? `/${fetchJob.total}` : ''} saved · status={fetchJob.status}
                {fetchJob.error ? ` · ${fetchJob.error}` : ''}
              </p>
            )}
          </div>
        </div>

        {book.synopsis && <p className="synopsis">{book.synopsis}</p>}

        <h2 className="toc-heading">Mục lục</h2>
        <div className="toc-folders">
          {groups.map((group, gi) => {
            const open = openFolders.has(gi);
            const savedInGroup = group.chapters.filter((c) => c.saved).length;
            return (
              <div key={group.start} className="toc-folder">
                <button
                  type="button"
                  className="toc-folder-head"
                  onClick={() =>
                    setOpenFolders((prev) => {
                      const next = new Set(prev);
                      if (next.has(gi)) next.delete(gi);
                      else next.add(gi);
                      return next;
                    })
                  }
                >
                  <span>
                    Chương {group.start}–{group.end} · {savedInGroup}/{group.chapters.length} saved
                  </span>
                  <span className="pull">{open ? '▾' : '▸'}</span>
                </button>
                {open && (
                  <div className="toc-list">
                    {group.chapters.map((ch) => (
                      <button
                        type="button"
                        key={ch.id}
                        className={`toc-item${ch.saved ? ' saved' : ' missing'}`}
                        disabled={!ch.saved}
                        title={ch.saved ? 'Open chapter' : 'Not downloaded yet — use Fetch missing'}
                        onClick={() => void openChapter(ch.id)}
                      >
                        <span className="toc-dot">{ch.saved ? '✓' : '·'}</span>
                        <span className="toc-num">{ch.id}.</span>
                        <span className="toc-title">{ch.title}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {reading && (
          <div className="reader-overlay" role="dialog" aria-modal="true" aria-label="Chapter reader">
            <div className="reader-panel">
              <div className="reader-head">
                <h3>
                  #{reading.id} — {reading.title}
                </h3>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setReader({ loading: false, chapter: null, error: '' })}
                >
                  ✕ Close
                </button>
              </div>
              {/* Chapter bodies come from our own crawler pipeline (sanitized by
                  the source cleaner), so this is trusted content, not user input. */}
              <div className="reader-body" dangerouslySetInnerHTML={{ __html: reading.body }} />
              <div className="reader-nav">
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={reading.id <= 1}
                  onClick={() => void openChapter(reading.id - 1)}
                >
                  ← Prev
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={reading.id >= book.total_chapters}
                  onClick={() => void openChapter(reading.id + 1)}
                >
                  Next →
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

