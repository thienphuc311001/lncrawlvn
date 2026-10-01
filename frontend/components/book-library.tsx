'use client';

import { useCallback, useEffect, useState } from 'react';

import BookDetail from './book-detail';
import BookCard from './book-card';
import Icon from './icon';

export type BookSummary = {
  book_id: string;
  title: string;
  author: string;
  cover_url: string;
  total_chapters: number;
  saved_count: number;
  saved_at: number;
};

export default function BookLibrary({ onGoExtract, initialBookId = null }: { onGoExtract: () => void; initialBookId?: string | null }) {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openBook, setOpenBook] = useState<string | null>(initialBookId);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('recent');

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/books');
      if (!res.ok) throw new Error(`Unable to load your books (HTTP ${res.status}).`);
      setBooks(await res.json());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  if (openBook) return <BookDetail bookId={openBook} onBack={() => { setOpenBook(null); window.scrollTo({ top: 0, behavior: 'instant' }); void reload(); }} />;

  const query = search.trim().toLocaleLowerCase();
  const filtered = books.filter(book => `${book.title} ${book.author}`.toLocaleLowerCase().includes(query))
    .sort((a, b) => sort === 'title' ? a.title.localeCompare(b.title) : b.saved_at - a.saved_at);

  return <section className="section" id="books">
    <div className="container">
      <div className="library-head workspace-heading">
        <div><p className="eyebrow">YOUR PERSONAL BOOKSHELF</p><h1>Your books, all together.</h1><p className="lead">Pick up a story, collect missing chapters, or take a book offline.</p></div>
        <button type="button" className="btn btn-primary" onClick={onGoExtract}><Icon name="link" size={18} /> Add a novel</button>
      </div>
      <div className="library-toolbar">
        <div className="library-search"><Icon name="search" size={18} /><input type="search" aria-label="Search books by title or author" placeholder="Search by title or author…" value={search} onChange={event => setSearch(event.target.value)} /></div>
        <span className="library-count" aria-live="polite">{filtered.length} {filtered.length === 1 ? 'book' : 'books'}</span>
        <select className="input library-sort" aria-label="Sort books" value={sort} onChange={event => setSort(event.target.value)}><option value="recent">Recently added</option><option value="title">Title A–Z</option></select>
        <button type="button" className="btn btn-secondary" disabled={loading} onClick={() => void reload()}><Icon name="refresh" size={16} /> {loading ? 'Loading…' : 'Refresh'}</button>
      </div>
      {error && <div className="library-notice" role="alert"><div><h3>We couldn’t load your library</h3><p>{error} Check that the backend is running, then try again.</p></div><button type="button" className="btn btn-secondary" disabled={loading} onClick={() => void reload()}>Try again</button></div>}
      {loading && books.length === 0 && !error && <div className="book-grid" role="status" aria-label="Loading library">{[0, 1, 2].map(n => <div key={n} className="book-skeleton" />)}</div>}
      {!loading && !error && books.length === 0 && <div className="library-empty library-empty-full"><span className="empty-book-icon"><Icon name="book" size={36} /></span><h2>Your next favorite starts here.</h2><p>Every novel you extract is saved here, chapter by chapter.</p><button type="button" className="btn btn-primary" onClick={onGoExtract}>Extract your first novel <Icon name="arrow" size={17} /></button></div>}
      {!error && books.length > 0 && filtered.length === 0 && <div className="library-empty library-empty-full"><Icon name="search" size={32} /><h2>No matching books</h2><p>Try a different title or author.</p><button type="button" className="btn btn-secondary" onClick={() => setSearch('')}>Clear search</button></div>}
      {!error && filtered.length > 0 && <section aria-labelledby="saved-books-heading"><h2 id="saved-books-heading" className="sr-only">Saved novels</h2><div className="book-grid">{filtered.map(book => <BookCard key={book.book_id} book={book} onOpen={() => { setOpenBook(book.book_id); window.scrollTo({ top: 0, behavior: 'instant' }); }} />)}</div></section>}
    </div>
  </section>;
}
