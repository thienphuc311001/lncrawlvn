'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

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

type UploadResult = {
  books: BookSummary[];
  errors: { filename: string; error: string }[];
  skipped: string[];
};

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export default function BookLibrary({ onGoExtract, initialBookId = null }: { onGoExtract: () => void; initialBookId?: string | null }) {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openBook, setOpenBook] = useState<string | null>(initialBookId);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('recent');
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const uploadInFlight = useRef(false);

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

  async function uploadBooks(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (uploadInFlight.current) return;
    setUploadError('');
    setUploadResult(null);
    if (selectedFiles.length === 0) {
      setUploadError('Choose at least one EPUB, TXT, or ZIP file.');
      return;
    }
    if (selectedFiles.reduce((total, file) => total + file.size, 0) > MAX_UPLOAD_BYTES) {
      setUploadError('The selected files exceed 50 MiB in total. Choose fewer or smaller files.');
      return;
    }
    uploadInFlight.current = true;
    setUploading(true);
    try {
      const body = new FormData();
      selectedFiles.forEach(file => body.append('files', file));
      const response = await fetch('/api/books/upload', { method: 'POST', body });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(typeof data?.detail === 'string' ? data.detail : `Unable to upload files (HTTP ${response.status}).`);
      }
      const result: UploadResult = await response.json();
      setUploadResult(result);
      if (result.books.length > 0) {
        setSearch('');
        setSort('recent');
      }
      if (result.books.length > 0 && result.errors.length === 0) {
        setSelectedFiles([]);
        if (fileInput.current) fileInput.current.value = '';
      }
      await reload();
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : String(e));
    } finally {
      uploadInFlight.current = false;
      setUploading(false);
    }
  }

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
      <form className="library-upload" aria-labelledby="library-upload-heading" aria-busy={uploading} onSubmit={uploadBooks}>
        <div>
          <h2 id="library-upload-heading">Upload your books</h2>
          <p id="library-upload-help" className="library-upload-help">Choose EPUB, TXT, or ZIP files, up to 50 MiB total per upload. In a ZIP, each EPUB or TXT file becomes one story; nested folders are supported. Nested ZIPs are not imported.</p>
        </div>
        <div className="library-upload-controls">
          <div className="field">
            <label htmlFor="library-upload-files">Book files</label>
            <input ref={fileInput} id="library-upload-files" className="input library-upload-input" type="file" accept=".epub,.txt,.zip" multiple disabled={uploading} aria-describedby={`library-upload-help${uploadError ? ' library-upload-error' : ''}`} onChange={event => {
              setSelectedFiles(Array.from(event.target.files ?? []));
              setUploadError('');
              setUploadResult(null);
            }} />
          </div>
          <button type="submit" className="btn btn-primary" disabled={uploading || selectedFiles.length === 0}>{uploading && <span className="spinner" aria-hidden="true" />}{uploading ? 'Importing…' : 'Upload to library'}</button>
        </div>
        {selectedFiles.length > 0 && <div className="library-upload-selection"><p>{selectedFiles.length} {selectedFiles.length === 1 ? 'file' : 'files'} selected · {(selectedFiles.reduce((total, file) => total + file.size, 0) / (1024 * 1024)).toFixed(2)} MiB</p><ul>{selectedFiles.map((file, index) => <li key={`${file.name}-${index}`}>{file.name}</li>)}</ul></div>}
        <div className="library-upload-status" role="status" aria-atomic="true">
          {uploading && <p>Uploading and saving your books. Please wait before submitting again.</p>}
          {!uploading && uploadResult && <p className={uploadResult.books.length === 0 ? 'error-text' : ''}>{uploadResult.books.length === 0 ? 'No books were imported.' : `Imported ${uploadResult.books.length} ${uploadResult.books.length === 1 ? 'book' : 'books'} to your library.`}{uploadResult.errors.length > 0 ? ` ${uploadResult.errors.length} ${uploadResult.errors.length === 1 ? 'file or story failed' : 'files or stories failed'}.` : ''}{uploadResult.skipped.length > 0 ? ` Skipped ${uploadResult.skipped.length} unsupported ${uploadResult.skipped.length === 1 ? 'file' : 'files'}.` : ''}</p>}
        </div>
        {uploadError && <p id="library-upload-error" className="error-text" role="alert">{uploadError} Your file selection has been kept so you can retry.</p>}
        {!uploading && uploadResult && <div className="library-upload-outcome">
          {uploadResult.errors.length > 0 && <div><h3>Files or stories that failed</h3><ul>{uploadResult.errors.map((failure, index) => <li key={`${failure.filename}-${index}`}><strong>{failure.filename}</strong>: {failure.error}</li>)}</ul><p>Your file selection has been kept so you can retry. Books already imported will not be overwritten.</p></div>}
          {uploadResult.skipped.length > 0 && <details><summary>Skipped files ({uploadResult.skipped.length})</summary><ul>{uploadResult.skipped.map((filename, index) => <li key={`${filename}-${index}`}>{filename}</li>)}</ul></details>}
        </div>}
      </form>
      <div className="library-toolbar">
        <div className="library-search"><Icon name="search" size={18} /><input type="search" aria-label="Search books by title or author" placeholder="Search by title or author…" value={search} onChange={event => setSearch(event.target.value)} /></div>
        <span className="library-count" aria-live="polite">{filtered.length} {filtered.length === 1 ? 'book' : 'books'}</span>
        <select className="input library-sort" aria-label="Sort books" value={sort} onChange={event => setSort(event.target.value)}><option value="recent">Recently added</option><option value="title">Title A–Z</option></select>
        <button type="button" className="btn btn-secondary" disabled={loading} onClick={() => void reload()}><Icon name="refresh" size={16} /> {loading ? 'Loading…' : 'Refresh'}</button>
      </div>
      {error && <div className="library-notice" role="alert"><div><h3>We couldn’t load your library</h3><p>{error} Check that the backend is running, then try again.</p></div><button type="button" className="btn btn-secondary" disabled={loading} onClick={() => void reload()}>Try again</button></div>}
      {loading && books.length === 0 && !error && <div className="book-grid" role="status" aria-label="Loading library">{[0, 1, 2].map(n => <div key={n} className="book-skeleton" />)}</div>}
      {!loading && !error && books.length === 0 && <div className="library-empty library-empty-full"><span className="empty-book-icon"><Icon name="book" size={36} /></span><h2>Your next favorite starts here.</h2><p>Upload EPUB, TXT, or ZIP files above, or extract a novel from a website. Your stories are saved here, chapter by chapter.</p><button type="button" className="btn btn-primary" onClick={onGoExtract}>Extract your first novel <Icon name="arrow" size={17} /></button></div>}
      {!error && books.length > 0 && filtered.length === 0 && <div className="library-empty library-empty-full"><Icon name="search" size={32} /><h2>No matching books</h2><p>Try a different title or author.</p><button type="button" className="btn btn-secondary" onClick={() => setSearch('')}>Clear search</button></div>}
      {!error && filtered.length > 0 && <section aria-labelledby="saved-books-heading"><h2 id="saved-books-heading" className="sr-only">Saved novels</h2><div className="book-grid">{filtered.map(book => <BookCard key={book.book_id} book={book} onOpen={() => { setOpenBook(book.book_id); window.scrollTo({ top: 0, behavior: 'instant' }); }} />)}</div></section>}
    </div>
  </section>;
}
