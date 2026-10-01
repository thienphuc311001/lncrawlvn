import Icon from './icon';
import type { BookSummary } from './book-library';

export default function BookCard({ book, onOpen }: { book: BookSummary; onOpen: () => void }) {
  const pct = book.total_chapters > 0 ? Math.min(100, Math.round(book.saved_count / book.total_chapters * 100)) : 0;
  const complete = book.total_chapters > 0 && book.saved_count >= book.total_chapters;
  return <button type="button" className="book-card" onClick={onOpen}>
    <div className="book-cover">
      <span className="book-cover-fallback"><Icon name="book" size={28} /></span>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`/api/books/${encodeURIComponent(book.book_id)}/cover`} alt="" loading="lazy" onError={event => { event.currentTarget.style.display = 'none'; }} />
    </div>
    <div className="book-info">
      <span className={`book-state${complete ? ' complete' : ''}`}>{complete ? <><Icon name="check" size={12} /> Ready to read</> : 'In your library'}</span>
      <h3 className="book-title">{book.title}</h3>
      <p className="book-author">{book.author || 'Unknown author'}</p>
      <p className="book-progress">{book.saved_count.toLocaleString()} / {book.total_chapters.toLocaleString()} chapters <span>{pct}%</span></p>
      <div className="progress-track" aria-hidden="true"><div className="progress-fill" style={{ width: `${pct}%` }} /></div>
    </div>
  </button>;
}
