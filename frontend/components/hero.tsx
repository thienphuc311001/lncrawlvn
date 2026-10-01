'use client';

import { useEffect, useState } from 'react';

import { useJobRunner, type ExtractOptions } from './job-runner';
import SettingsModal from './settings-modal';
import Icon from './icon';
import BookCard from './book-card';
import type { BookSummary } from './book-library';

const EXAMPLE_URL = 'https://xtruyen.vn/truyen/huyen-giam-tien-toc/';

export default function Hero({ onGoBooks }: { onGoBooks: (bookId?: string) => void }) {
  const [url, setUrl] = useState('');
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [crawlOptions, setCrawlOptions] = useState<ExtractOptions>({});
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [libraryState, setLibraryState] = useState<'loading' | 'ready' | 'error'>('loading');
  const { job, jobStatus, startJob } = useJobRunner();
  const isRunning = job !== null && ['pending', 'running', 'stopping'].includes(jobStatus);
  const busy = isRunning || starting;

  useEffect(() => {
    const controller = new AbortController();
    setLibraryState('loading');
    fetch('/api/books', { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('Unable to load library');
      const list: BookSummary[] = await response.json();
      setBooks(list.sort((a, b) => b.saved_at - a.saved_at));
      setLibraryState('ready');
    }).catch(() => { if (!controller.signal.aborted) setLibraryState('error'); });
    return () => controller.abort();
  }, [jobStatus]);

  const start = async () => {
    if (busy) return;
    const target = url.trim();
    try {
      const parsed = new URL(target);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error();
    } catch {
      setStartError('Enter a complete novel URL starting with https:// or http://.');
      document.getElementById('novel-url')?.focus();
      return;
    }
    setStartError('');
    setStarting(true);
    try {
      await startJob(target, crawlOptions);
    } catch (error) {
      setStartError(error instanceof Error ? error.message : String(error));
    } finally {
      setStarting(false);
    }
  };

  return (
    <section className="section extract-workspace" id="hero">
      <div className="container">
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">A NEW CHAPTER STARTS HERE</p>
            <h1>Your stories, beyond the browser.</h1>
            <p className="lead">Bring the novels you love into a library of your own.</p>
          </div>
          <span className="source-label"><span aria-hidden="true" /> Chinese & Vietnamese sources</span>
        </div>

        <div className="extract-grid">
          <div className="extract-card">
            <div className="extract-card-head">
              <span className="feature-icon"><Icon name="link" size={22} /></span>
              <div><h2>Add a novel</h2><p>Start with a link. We’ll take care of the chapters.</p></div>
            </div>
            <form onSubmit={event => { event.preventDefault(); void start(); }} noValidate>
              <div className="field">
                <label htmlFor="novel-url">Novel URL</label>
                <div className={`url-field${startError ? ' invalid' : ''}`}>
                  <Icon name="link" size={18} />
                  <input type="url" id="novel-url" placeholder="https://example.com/novel/your-story"
                    autoComplete="url" spellCheck={false} value={url} disabled={busy}
                    aria-invalid={Boolean(startError)} aria-describedby={startError ? 'extract-error' : 'url-hint'}
                    onChange={event => { setUrl(event.target.value); setStartError(''); }} />
                </div>
                <p id="url-hint" className="field-hint">Use the novel’s main page, rather than an individual chapter.</p>
              </div>
              {startError && <p id="extract-error" role="alert" className="error-text extract-error">{startError}</p>}
              <div className="extract-actions">
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? <span className="spinner" aria-hidden="true" /> : <Icon name="download" size={18} />}
                  {starting ? 'Starting…' : isRunning && jobStatus === 'stopping' ? 'Stopping crawl…' : isRunning ? 'Crawling…' : 'Extract novel'}
                  {!busy && <Icon name="arrow" size={17} />}
                </button>
                <button type="button" className="btn btn-ghost example-button" disabled={busy}
                  onClick={() => { setUrl(EXAMPLE_URL); setStartError(''); document.getElementById('novel-url')?.focus(); }}>Try an example <Icon name="arrow" size={15} /></button>
                <button type="button" className="btn btn-ghost settings-button" onClick={() => setShowSettings(true)}><Icon name="settings" size={17} /> Settings</button>
              </div>
            </form>
            <div className="extract-card-foot"><Icon name="check" size={15} /><span>{crawlOptions.save === false ? 'Automatic library saving is turned off in Settings.' : 'Chapters are saved to your library as they download.'}</span></div>
          </div>
          <aside className="workflow-card" aria-label="How extraction works">
            <span className="workflow-kicker">FROM LINK TO LIBRARY</span>
            <h2>A whole story. <br />Ready to go.</h2>
            <ol className="workflow-steps">
              <li><span className="step-number">01</span><div><h3>Find your novel</h3><p>Paste a link from a supported source.</p></div></li>
              <li><span className="step-number">02</span><div><h3>Collect the chapters</h3><p>Follow the progress as your library grows.</p></div></li>
              <li><span className="step-number">03</span><div><h3>Take it with you</h3><p>Read here or export for offline reading.</p></div></li>
            </ol>
            <div className="workflow-formats"><Icon name="book" size={17} /><span>Your next read, your way</span><span className="format-tag">EPUB</span><span className="format-tag">TXT</span></div>
          </aside>
        </div>

        <section className="recent-library" aria-labelledby="recent-heading">
          <div className="section-heading"><div><p className="eyebrow">YOUR PERSONAL BOOKSHELF</p><h2 id="recent-heading">Recently added {libraryState === 'ready' && <span className="count-badge">{books.length}</span>}</h2></div>
            <button type="button" className="btn btn-ghost" onClick={() => onGoBooks()}>View library <Icon name="arrow" size={17} /></button></div>
          {libraryState === 'loading' ? <div className="book-grid" role="status" aria-label="Loading recent books">{[0, 1, 2].map(n => <div key={n} className="book-skeleton" />)}</div>
            : libraryState === 'error' ? <div className="library-notice" role="status"><Icon name="book" size={24} /><div><h3>Your library is temporarily unavailable</h3><p>Open Books to retry loading your saved novels.</p></div><button type="button" className="btn btn-secondary" onClick={() => onGoBooks()}>Open library</button></div>
            : books.length ? <div className="book-grid">{books.slice(0, 3).map(book => <BookCard key={book.book_id} book={book} onOpen={() => onGoBooks(book.book_id)} />)}</div>
            : <div className="library-empty"><span className="empty-book-icon"><Icon name="book" size={32} /></span><div><h3>A bookshelf waiting for your stories.</h3><p>Extract your first novel above. You’ll find every saved chapter here.</p></div><span className="empty-label">THE FIRST OF MANY</span></div>}
        </section>
        <div className="workspace-bottom"><Icon name="book" size={16} /><span>A place to collect, translate, and return to your favorite stories.</span></div>
      </div>
      <SettingsModal open={showSettings} onClose={() => setShowSettings(false)} onCrawlOptionsChange={setCrawlOptions} />
    </section>
  );
}
