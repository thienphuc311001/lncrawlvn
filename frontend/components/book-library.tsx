'use client';

import { useCallback, useEffect, useState } from 'react';

import BookDetail from './book-detail';

const API_BASE = 'http://127.0.0.1:8000';

export type BookSummary = {
  book_id: string;
  title: string;
  author: string;
  cover_url: string;
  total_chapters: number;
  saved_count: number;
  saved_at: number;
};

export default function BookLibrary({ onGoExtract }: { onGoExtract: () => void }) {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openBook, setOpenBook] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/books`);
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      setBooks(await res.json());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (openBook) {
    return (
      <BookDetail
        bookId={openBook}
        onBack={() => {
          setOpenBook(null);
          void reload();
        }}
      />
    );
  }

  return (
    <section className="section" id="books">
      <div className="container">
        <div className="library-head">
          <div>
            <p className="eyebrow">LIBRARY</p>
            <h1>Your books</h1>
          </div>
          <div style={{ display: 'flex', gap: '12px' }}>
            <button type="button" className="btn btn-ghost" onClick={() => void reload()}>
              Refresh
            </button>
            <button type="button" className="btn btn-primary" onClick={onGoExtract}>
              + Crawl a novel
            </button>
          </div>
        </div>

        {loading && <p className="muted">Loading library…</p>}
        {error && <p className="muted error-text">✗ {error} — is the backend running?</p>}
        {!loading && !error && books.length === 0 && (
          <div className="book-card empty-state">
            <h3>No books yet</h3>
            <p className="muted">
              Extract a novel first — every crawl is saved here automatically, chapter by chapter.
            </p>
          </div>
        )}

        <div className="book-grid">
          {books.map((book) => {
            const pct =
              book.total_chapters > 0
                ? Math.round((book.saved_count / book.total_chapters) * 100)
                : 0;
            return (
              <button
                type="button"
                key={book.book_id}
                className="book-card"
                onClick={() => setOpenBook(book.book_id)}
              >
                <div className="book-cover">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`${API_BASE}/api/books/${encodeURIComponent(book.book_id)}/cover`}
                    alt=""
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = 'none';
                    }}
                  />
                </div>
                <div className="book-info">
                  <h3 className="book-title">{book.title}</h3>
                  {book.author && <p className="muted">{book.author}</p>}
                  <p className="book-progress">
                    {book.saved_count}/{book.total_chapters} chapters · {pct}%
                  </p>
                  <div className="progress-track">
                    <div className="progress-fill" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
