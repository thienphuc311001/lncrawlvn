'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react';

import { useJobRunner } from './job-runner';
import useDialog from './use-dialog';

const API_BASE = '';
const POLL_INTERVAL_MS = 800;
/** Mirrors CHAPTERS_PER_FOLDER in lncrawl/library.py */
const FOLDER_SIZE = 100;
/** Mirrors DEFAULT_EXPORT_CHUNK / MAX_EXPORT_CHUNK in lncrawl/library.py */
const DEFAULT_EXPORT_PER_FILE = 100;
const MAX_EXPORT_PER_FILE = 10000;

type TocChapter = { id: number; title: string; url: string; saved: boolean };

type Book = {
  book_id: string;
  title: string;
  author: string;
  cover_url: string;
  url: string;
  total_chapters: number;
  saved_count: number;
  synopsis: string;
  tags: string[];
  chapters: TocChapter[];
};

type ActiveCrawlJob = {
  job_id: string;
  status: 'pending' | 'running' | 'stopping';
  title?: string;
  url?: string;
};

type ChapterContent = { id: number; title: string; url: string; body: string };

type ReaderState = { loading: boolean; chapter: ChapterContent | null; error: string };

type ChapterTranslation = {
  status: string;
  translated: { title: string; paragraphs: string[] } | null;
  job: {
    job_id: string;
    status: string;
    stage?: string;
    stage_label?: string;
    error?: string;
    error_detail?: unknown;
    manual_review?: {
      chapter: number; paragraph_id: string; fingerprint: string;
      raw: string; current_text: string; findings: unknown[];
    };
  } | null;
  error: string | null;
  stale: boolean;
  entry_count: number;
  active_chapter_id: number | null;
};

type BookDictionaryInfo = { entry_count: number; imported: boolean; active_chapter_id: number | null };

function readerError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === 'string' ? error : JSON.stringify(error) ?? String(error);
}

async function readerRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(readerError(data?.detail ?? `API returned ${response.status}`));
  return data as T;
}

function folderRange(chapterId: number): { start: number; end: number } {
  const start = Math.floor((chapterId - 1) / FOLDER_SIZE) * FOLDER_SIZE + 1;
  return { start, end: start + FOLDER_SIZE - 1 };
}

function chapterHtmlToText(html: string): string {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  parsed.querySelectorAll('script, style, noscript').forEach((node) => node.remove());
  const blockTags = new Set([
    'ADDRESS', 'ARTICLE', 'BLOCKQUOTE', 'DD', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION',
    'FIGURE', 'FOOTER', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'LI', 'MAIN', 'OL',
    'P', 'PRE', 'SECTION', 'TABLE', 'TR', 'UL',
  ]);
  const read = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
    if (!(node instanceof Element)) return '';
    if (node.tagName === 'BR') return '\n';
    const content = Array.from(node.childNodes, read).join('');
    return blockTags.has(node.tagName) ? `\n${content}\n` : content;
  };
  return read(parsed.body)
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]*\n[ \t]*/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export default function BookDetail({ bookId, onBack }: { bookId: string; onBack: () => void }) {
  const { trackJob } = useJobRunner();
  const [book, setBook] = useState<Book | null>(null);
  const [error, setError] = useState('');
  const [openFolders, setOpenFolders] = useState<Set<number>>(() => new Set());
  const [reader, setReader] = useState<ReaderState>({ loading: false, chapter: null, error: '' });
  const [crawlMode, setCrawlMode] = useState<'missing' | 'overwrite' | null>(null);
  const [exportError, setExportError] = useState('');
  const [exporting, setExporting] = useState<'epub' | 'txt' | null>(null);
  const [checkingExport, setCheckingExport] = useState(false);
  const [needsChapterNumbers, setNeedsChapterNumbers] = useState(false);
  const [includeChapterNumber, setIncludeChapterNumber] = useState<boolean | null>(null);
  /** Format whose "chapters per file" dialog is open, if any. */
  const [exportDialog, setExportDialog] = useState<'epub' | 'txt' | null>(null);
  const [perFileInput, setPerFileInput] = useState(String(DEFAULT_EXPORT_PER_FILE));
  const [dialogError, setDialogError] = useState('');
  const [translatingGroup, setTranslatingGroup] = useState<number | null>(null);
  const [titleError, setTitleError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [activeJobs, setActiveJobs] = useState<ActiveCrawlJob[] | null>(null);
  const [jobsError, setJobsError] = useState('');
  const [stopError, setStopError] = useState('');
  const [stoppingJobId, setStoppingJobId] = useState<string | null>(null);
  const [deletingChapter, setDeletingChapter] = useState(false);
  const [translation, setTranslation] = useState<ChapterTranslation | null>(null);
  const [dictionaryInfo, setDictionaryInfo] = useState<BookDictionaryInfo | null>(null);
  const [readerView, setReaderView] = useState<'raw' | 'translated'>('raw');
  const [readerInfoError, setReaderInfoError] = useState('');
  const [readerBusy, setReaderBusy] = useState(false);
  const [copyingChapterId, setCopyingChapterId] = useState<number | null>(null);
  const [copiedChapterId, setCopiedChapterId] = useState<number | null>(null);
  const [copyError, setCopyError] = useState<{ chapterId: number; message: string } | null>(null);
  const copyTimer = useRef<number | undefined>(undefined);
  const readerEpoch = useRef(0);
  const readerRequestId = useRef(0);
  const lastTranslationStatus = useRef<string | null>(null);
  const reviewTextRef = useRef<HTMLTextAreaElement | null>(null);
  const bookPath = `/api/books/${encodeURIComponent(bookId)}`;

  const closeReader = useCallback(() => {
    readerEpoch.current++;
    readerRequestId.current++;
    lastTranslationStatus.current = null;
    setReader({ loading: false, chapter: null, error: '' });
    setTranslation(null);
    setDictionaryInfo(null);
    setReaderInfoError('');
    setReaderBusy(false);
    setReaderView('raw');
  }, []);

  useEffect(() => () => {
    readerEpoch.current++;
    readerRequestId.current++;
    window.clearTimeout(copyTimer.current);
  }, [bookId]);

  const readerDialogRef = useDialog(Boolean(reader.chapter), closeReader);
  const exportDialogRef = useDialog(Boolean(exportDialog), () => closeExportDialog());

  const applyTranslation = useCallback((result: ChapterTranslation) => {
    if (result.status === 'done' && !result.stale && lastTranslationStatus.current !== 'done') {
      setReaderView('translated');
    }
    if (result.stale || (result.job && result.status !== 'done')) setReaderView('raw');
    lastTranslationStatus.current = result.status;
    setTranslation(result);
    setDictionaryInfo(current => current && ({
      ...current, entry_count: result.entry_count, active_chapter_id: result.active_chapter_id,
    }));
    setReaderInfoError('');
  }, []);

  const refreshTranslation = useCallback(async (chapterId: number, epoch: number, isLive: () => boolean = () => true) => {
    const requestId = ++readerRequestId.current;
    try {
      const result = await readerRequest<ChapterTranslation>(`${bookPath}/chapters/${chapterId}/translation`);
      if (readerEpoch.current === epoch && readerRequestId.current === requestId && isLive()) applyTranslation(result);
    } catch (e) {
      if (readerEpoch.current === epoch && readerRequestId.current === requestId && isLive()) {
        setReaderInfoError(readerError(e));
      }
    }
  }, [applyTranslation, bookPath]);

  const loadReaderInfo = useCallback(async (chapterId: number, epoch: number) => {
    const requestId = ++readerRequestId.current;
    try {
      const [dictionary, result] = await Promise.all([
        readerRequest<BookDictionaryInfo>(`${bookPath}/dictionary`),
        readerRequest<ChapterTranslation>(`${bookPath}/chapters/${chapterId}/translation`),
      ]);
      if (readerEpoch.current !== epoch || readerRequestId.current !== requestId) return;
      setDictionaryInfo(dictionary);
      applyTranslation(result);
    } catch (e) {
      if (readerEpoch.current === epoch && readerRequestId.current === requestId) {
        setReaderInfoError(readerError(e));
      }
    }
  }, [applyTranslation, bookPath]);

  useEffect(() => {
    const chapterId = reader.chapter?.id;
    if (chapterId === undefined || readerBusy || !translation ||
        !(['pending', 'running'].includes(translation.status) || translation.active_chapter_id !== null)) return;
    const epoch = readerEpoch.current;
    let live = true;
    let polling = false;
    const timer = window.setInterval(() => {
      if (polling) return;
      polling = true;
      void refreshTranslation(chapterId, epoch, () => live).finally(() => { polling = false; });
    }, 1500);
    return () => { live = false; window.clearInterval(timer); };
  }, [reader.chapter?.id, readerBusy, translation?.status, translation?.active_chapter_id, refreshTranslation]);
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
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let hadActive = false;
    setActiveJobs(null);
    setJobsError('');
    const refresh = async () => {
      try {
        const res = await fetch(`${bookPath}/jobs`);
        if (!res.ok) throw new Error(`API returned ${res.status}`);
        const jobs: ActiveCrawlJob[] = await res.json();
        if (cancelled) return;
        setActiveJobs((previous) => jobs.map((item) => {
          const older = previous?.find((entry) => entry.job_id === item.job_id);
          return older?.status === 'stopping' && item.status !== 'stopping'
            ? { ...item, status: 'stopping' }
            : item;
        }));
        setJobsError('');
        if (hadActive && jobs.length === 0) void load();
        hadActive = jobs.length > 0;
      } catch (e) {
        if (cancelled) return;
        setActiveJobs(null);
        setJobsError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void refresh(), POLL_INTERVAL_MS);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [bookPath, load]);

  const stopCrawl = useCallback(async (jobId: string) => {
    if (stoppingJobId) return;
    setStopError('');
    setStoppingJobId(jobId);
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST' });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `API returned ${res.status}`);
      }
      const stopped: { status: string } = await res.json();
      if (stopped.status === 'stopping') {
        setActiveJobs((jobs) => jobs?.map((item) =>
          item.job_id === jobId ? { ...item, status: 'stopping' } : item,
        ) ?? null);
      }
    } catch (e) {
      setStopError(e instanceof Error ? e.message : String(e));
    } finally {
      setStoppingJobId(null);
    }
  }, [stoppingJobId]);


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
      const epoch = ++readerEpoch.current;
      readerRequestId.current++;
      lastTranslationStatus.current = null;
      setReader({ loading: true, chapter: null, error: '' });
      setTranslation(null);
      setDictionaryInfo(null);
      setReaderView('raw');
      setReaderBusy(false);
      setReaderInfoError('');
      try {
        const chapter = await readerRequest<ChapterContent>(`${bookPath}/chapters/${chapterId}`);
        if (readerEpoch.current !== epoch) return;
        setReader({ loading: false, chapter, error: '' });
        void loadReaderInfo(chapterId, epoch);
      } catch (e) {
        if (readerEpoch.current === epoch) {
          setReader({ loading: false, chapter: null, error: readerError(e) });
        }
      }
    },
    [bookPath, loadReaderInfo],
  );

  const copyChapter = useCallback(async (chapter: TocChapter) => {
    if (copyingChapterId !== null) return;
    setCopyingChapterId(chapter.id);
    setCopiedChapterId(null);
    setCopyError(null);
    window.clearTimeout(copyTimer.current);
    try {
      const content = await readerRequest<ChapterContent>(`${bookPath}/chapters/${chapter.id}`);
      const body = chapterHtmlToText(content.body);
      const text = [`${chapter.id}. ${chapter.title || content.title}`.trim(), body]
        .filter(Boolean)
        .join('\n\n');
      await navigator.clipboard.writeText(text);
      setCopiedChapterId(chapter.id);
      copyTimer.current = window.setTimeout(() => setCopiedChapterId(null), 1800);
    } catch (e) {
      setCopyError({ chapterId: chapter.id, message: readerError(e) });
    } finally {
      setCopyingChapterId(null);
    }
  }, [bookPath, copyingChapterId]);

  const copyReaderChapter = useCallback(async () => {
    const chapter = reader.chapter;
    if (!chapter || copyingChapterId !== null) return;
    const activeId = translation?.active_chapter_id ?? dictionaryInfo?.active_chapter_id ?? null;
    const translatedContent = readerView === 'translated' ? translation?.translated : null;
    const translationToCopy = translatedContent && !translation?.stale && activeId !== chapter.id
      ? translatedContent
      : null;
    const title = translationToCopy?.title ?? chapter.title;
    const body = translationToCopy?.paragraphs.join('\n\n') ?? chapterHtmlToText(chapter.body);
    setCopyingChapterId(chapter.id);
    setCopiedChapterId(null);
    setCopyError(null);
    window.clearTimeout(copyTimer.current);
    try {
      await navigator.clipboard.writeText([`${chapter.id}. ${title}`.trim(), body].filter(Boolean).join('\n\n'));
      setCopiedChapterId(chapter.id);
      copyTimer.current = window.setTimeout(() => setCopiedChapterId(null), 1800);
    } catch (e) {
      setCopyError({ chapterId: chapter.id, message: readerError(e) });
    } finally {
      setCopyingChapterId(null);
    }
  }, [copyingChapterId, dictionaryInfo?.active_chapter_id, reader.chapter, readerView, translation]);

  const importDictionary = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    const chapterId = reader.chapter?.id;
    if (!file || chapterId === undefined || readerBusy) return;
    const epoch = readerEpoch.current;
    setReaderInfoError('');
    setReaderBusy(true);
    try {
      const dictionary = JSON.parse(await file.text()) as unknown;
      if (readerEpoch.current !== epoch) return;
      if (dictionaryInfo && (dictionaryInfo.imported || dictionaryInfo.entry_count > 0) && !window.confirm(
        'Import sẽ thay toàn bộ dictionary hiện tại của truyện (bao gồm thuật ngữ đã tích lũy). Tiếp tục?',
      )) return;
      await readerRequest(`${bookPath}/dictionary`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dictionary }),
      });
      if (readerEpoch.current === epoch) await loadReaderInfo(chapterId, epoch);
    } catch (e) {
      if (readerEpoch.current === epoch) setReaderInfoError(readerError(e));
    } finally {
      if (readerEpoch.current === epoch) setReaderBusy(false);
    }
  }, [bookPath, dictionaryInfo?.imported, loadReaderInfo, reader.chapter?.id, readerBusy]);

  const translateReaderChapter = useCallback(async (forceRefresh = false) => {
    const chapterId = reader.chapter?.id;
    if (chapterId === undefined || readerBusy || translation?.active_chapter_id !== null &&
        translation?.active_chapter_id !== undefined) return;
    const epoch = readerEpoch.current;
    readerRequestId.current++;
    setReaderBusy(true);
    setReaderInfoError('');
    setReaderView('raw');
    try {
      await readerRequest(`${bookPath}/chapters/${chapterId}/translate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh: forceRefresh || Boolean(translation?.translated) }),
      });
      if (readerEpoch.current === epoch) await refreshTranslation(chapterId, epoch);
    } catch (e) {
      if (readerEpoch.current === epoch) setReaderInfoError(readerError(e));
    } finally {
      if (readerEpoch.current === epoch) setReaderBusy(false);
    }
  }, [bookPath, reader.chapter?.id, readerBusy, refreshTranslation, translation]);

  const readerJobAction = useCallback(async (action: 'resume' | 'cancel' | 'review', decision?: 'accept' | 'replace', text?: string) => {
    const chapterId = reader.chapter?.id;
    const job = translation?.job;
    if (chapterId === undefined || !job || readerBusy) return;
    if (decision === 'accept' && !window.confirm(
      'Giữ nguyên đoạn dịch dù còn lỗi được liệt kê? Quyết định này sẽ được ghi nhận.',
    )) return;
    const epoch = readerEpoch.current;
    readerRequestId.current++;
    setReaderBusy(true);
    setReaderInfoError('');
    try {
      const review = job.manual_review;
      await readerRequest(`/api/translation/jobs/${encodeURIComponent(job.job_id)}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(action === 'review' && review ? {
          chapter: review.chapter, paragraph_id: review.paragraph_id,
          fingerprint: review.fingerprint, action: decision,
          ...(decision === 'replace' ? { text: text ?? '' } : {}),
        } : {}),
      });
      if (readerEpoch.current === epoch) await refreshTranslation(chapterId, epoch);
    } catch (e) {
      if (readerEpoch.current === epoch) setReaderInfoError(readerError(e));
    } finally {
      if (readerEpoch.current === epoch) setReaderBusy(false);
    }
  }, [reader.chapter?.id, readerBusy, refreshTranslation, translation?.job]);

  const startCrawl = useCallback(async (mode: 'missing' | 'overwrite') => {
    if (crawlMode || !book || activeJobs === null || activeJobs.length > 0) return;
    if (mode === 'overwrite' && !window.confirm(
      `Tải lại toàn bộ ${book.total_chapters} chương của "${book.title}" và ghi đè các chương đã lưu?`,
    )) return;
    setExportError('');
    setCrawlMode(mode);
    try {
      const res = await fetch(
        mode === 'overwrite'
          ? `${API_BASE}/api/extract`
          : `${API_BASE}/api/books/${encodeURIComponent(bookId)}/fetch-missing`,
        mode === 'overwrite'
          ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ url: book.url, overwrite: true }) }
          : { method: 'POST' },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `API returned ${res.status}`);
      // Console dock polls and streams this job's logs from anywhere in the UI.
      trackJob(data.job_id, `${mode === 'overwrite' ? 'Re-crawl & overwrite' : 'Fetch missing'} — ${book.title}`);
      setActiveJobs((jobs) => [...(jobs ?? []), { job_id: data.job_id, status: data.status, title: book.title }]);

      let status: string = data.status;
      if (status === 'failed' && data.error) setExportError(data.error);
      while (status === 'running' || status === 'pending' || status === 'stopping') {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, POLL_INTERVAL_MS);
        await promise;
        const poll = await fetch(`${API_BASE}/api/jobs/${data.job_id}`);
        if (!poll.ok) throw new Error(`Job poll returned ${poll.status}`);
        const job = await poll.json();
        status = job.status;
        if (job.status === 'failed' && job.error) setExportError(job.error);
      }
      await load();
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      setCrawlMode(null);
    }
  }, [activeJobs, book, bookId, crawlMode, load, trackJob]);

  const translateTitles = useCallback(async (group: { start: number; chapters: TocChapter[] }) => {
    setTranslatingGroup(group.start);
    setTitleError('');
    try {
      const res = await fetch(
        `${API_BASE}/api/books/${encodeURIComponent(bookId)}/translate-titles`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            chapters: group.chapters.map(({ id, title }) => ({ id, title })),
          }),
        },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `API returned ${res.status}`);
      const titles = new Map<number, string>(
        data.chapters.map((chapter: { id: number; title: string }) => [chapter.id, chapter.title]),
      );
      setBook((current) => current && ({
        ...current,
        chapters: current.chapters.map((chapter) => (
          titles.has(chapter.id) ? { ...chapter, title: titles.get(chapter.id)! } : chapter
        )),
      }));
    } catch (e) {
      setTitleError(e instanceof Error ? e.message : String(e));
    } finally {
      setTranslatingGroup(null);
    }
  }, [bookId]);

  const exportBook = useCallback(
    async (format: 'epub' | 'txt', chaptersPerFile: number, numberChapters: boolean) => {
      setExporting(format);
      setExportError('');
      try {
        const res = await fetch(
          `${API_BASE}/api/books/${encodeURIComponent(bookId)}/export` +
            `?format=${format}&per_file=${chaptersPerFile}&include_chapter_number=${numberChapters}`,
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

  const openExportDialog = useCallback(async (format: 'epub' | 'txt') => {
    if (checkingExport || exporting !== null) return;
    setCheckingExport(true);
    setExportError('');
    try {
      const { needs_chapter_numbers } = await readerRequest<{ needs_chapter_numbers: boolean }>(
        `${bookPath}/export-options?format=${format}`,
      );
      setNeedsChapterNumbers(needs_chapter_numbers);
      setIncludeChapterNumber(needs_chapter_numbers ? null : false);
      setDialogError('');
      setExportDialog(format);
    } catch (e) {
      setExportError(readerError(e));
    } finally {
      setCheckingExport(false);
    }
  }, [bookPath, checkingExport, exporting]);

  const closeExportDialog = useCallback(() => {
    setExportDialog(null);
    setDialogError('');
  }, []);

  /** Files the export would produce at the typed size (chunks with ≥1 saved chapter). */
  const estimatedFiles = useMemo(() => {
    if (!book) return 0;
    const perFile = Number(perFileInput);
    if (!Number.isInteger(perFile) || perFile < 1 || perFile > MAX_EXPORT_PER_FILE) return 0;
    let files = 0;
    let currentChunk = -1;
    for (const ch of book.chapters) {
      if (!ch.saved) continue;
      const chunk = Math.floor((ch.id - 1) / perFile);
      if (chunk !== currentChunk) files += 1;
      currentChunk = chunk;
    }
    return files;
  }, [book, perFileInput]);

  const submitExport = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!exportDialog) return;
      const perFile = Number(perFileInput);
      if (!Number.isInteger(perFile) || perFile < 1 || perFile > MAX_EXPORT_PER_FILE) {
        setDialogError(
          `Số chương mỗi file phải là số nguyên trong khoảng 1–${MAX_EXPORT_PER_FILE}.`,
        );
        return;
      }
      if (needsChapterNumbers && includeChapterNumber === null) {
        setDialogError('Chọn Có hoặc Không cho số chương trong file xuất.');
        return;
      }
      const format = exportDialog;
      setExportDialog(null);
      setDialogError('');
      void exportBook(format, perFile, includeChapterNumber === true);
    },
    [exportBook, exportDialog, perFileInput, needsChapterNumbers, includeChapterNumber],
  );


  const deleteBook = useCallback(async () => {
    if (!book || activeJobs === null || activeJobs.length > 0 || crawlMode) return;
    const confirmed = window.confirm(
      `Xóa "${book.title}" khỏi library?\n\n` +
        `Mất ${book.saved_count}/${book.total_chapters} chương đã lưu, cover và exports. ` +
        `Hành động này không thể hoàn tác.`,
    );
    if (!confirmed) return;
    setDeleting(true);
    setExportError('');
    try {
      const res = await fetch(`${API_BASE}/api/books/${encodeURIComponent(bookId)}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `API returned ${res.status}`);
      }
      onBack();
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  }, [activeJobs, book, bookId, crawlMode, onBack]);

  /** Drop the saved chapter file so "Fetch missing" can re-download it. */
  const deleteChapter = useCallback(
    async (chapter: ChapterContent) => {
      if (deletingChapter) return;
      const confirmed = window.confirm(
        `Xóa chương ${chapter.id} khỏi library?\n\n` +
          `File nội dung của chương này sẽ bị xóa khỏi đĩa, mục lục vẫn giữ nguyên. ` +
          `Dùng "Fetch missing" để tải lại chương này từ nguồn khi cần.`,
      );
      if (!confirmed) return;
      setDeletingChapter(true);
      setExportError('');
      try {
        const res = await fetch(
          `${API_BASE}/api/books/${encodeURIComponent(bookId)}/chapters/${chapter.id}`,
          { method: 'DELETE' },
        );
        if (!res.ok) {
          const detail = await res.json().catch(() => null);
          throw new Error(detail?.detail || `API returned ${res.status}`);
        }
        // Close the reader and refresh the TOC so the chapter shows as missing.
        closeReader();
        await load();
      } catch (e) {
        setExportError(e instanceof Error ? e.message : String(e));
      } finally {
        setDeletingChapter(false);
      }
    },
    [bookId, closeReader, deletingChapter, load],
  );

  if (error && !book) {
    return (
      <section className="section">
        <div className="container">
          <div className="book-detail-back">
            <button type="button" className="btn btn-ghost" onClick={onBack}>
              ← Back to library
            </button>
          </div>
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
  const activeChapter = translation?.active_chapter_id ?? dictionaryInfo?.active_chapter_id ?? null;
  const showingTranslation = readerView === 'translated' && !!translation?.translated &&
    !translation.stale && activeChapter !== reading?.id;

  return (
    <section className="section">
      <div className="container">
        <div className="book-detail-back">
          <button type="button" className="btn btn-ghost" onClick={onBack}>
            ← Back to library
          </button>
        </div>

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
            <div className="active-crawl-jobs" role="status">
              {activeJobs === null && !jobsError && <p className="muted">Đang kiểm tra crawl của sách…</p>}
              {activeJobs?.map((active) => (
                <div className="active-crawl-job" key={active.job_id}>
                  <span>
                    Crawl {active.title || active.url || active.job_id} · {active.job_id} —{' '}
                    {active.status === 'stopping' ? 'Đang dừng (chờ ghi xong)' :
                      active.status === 'pending' ? 'Đang chờ' : 'Đang chạy'}
                  </span>
                  <button type="button" className="btn btn-ghost"
                    onClick={() => trackJob(active.job_id, active.title || active.url || active.job_id)}>
                    Mở console
                  </button>
                  <button type="button" className="btn btn-danger"
                    disabled={active.status === 'stopping' || stoppingJobId !== null}
                    onClick={() => void stopCrawl(active.job_id)}>
                    {active.status === 'stopping' ? 'Đang dừng…' :
                      stoppingJobId === active.job_id ? 'Đang gửi…' : 'Dừng crawl'}
                  </button>
                </div>
              ))}
              {activeJobs && activeJobs.length > 0 &&
                <p className="muted">Đóng console không dừng crawl. Chờ crawl dừng hẳn trước khi xóa sách.</p>}
            </div>
            {jobsError && <p className="error-text" role="alert">
              Không thể kiểm tra crawl đang chạy: {jobsError}. Chưa thể xóa sách.
            </p>}
            {stopError && <p className="error-text" role="alert">Không thể dừng crawl: {stopError}</p>}
            <div className="detail-actions">
              <button
                type="button"
                className="btn btn-primary"
                disabled={missingCount === 0 || crawlMode !== null || activeJobs === null || activeJobs.length > 0}
                onClick={() => void startCrawl('missing')}
              >
                {crawlMode === 'missing' ? 'Fetching…' : `Fetch missing (${missingCount})`}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!book.url || book.total_chapters === 0 || crawlMode !== null || activeJobs === null || activeJobs.length > 0}
                title="Tải lại toàn bộ chương và ghi đè nội dung đã lưu"
                onClick={() => void startCrawl('overwrite')}
              >
                {crawlMode === 'overwrite' ? 'Re-crawling…' : 'Re-crawl & overwrite'}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={checkingExport || exporting !== null || book.saved_count === 0}
                onClick={() => void openExportDialog('epub')}
              >
                {checkingExport ? 'Checking…' : exporting === 'epub' ? 'Packing…' : 'Export EPUB'}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={checkingExport || exporting !== null || book.saved_count === 0}
                onClick={() => void openExportDialog('txt')}
              >
                {checkingExport ? 'Checking…' : exporting === 'txt' ? 'Packing…' : 'Export TXT'}
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={deleting || crawlMode !== null || exporting !== null || activeJobs === null || activeJobs.length > 0}
                onClick={() => void deleteBook()}
              >
                {deleting ? 'Đang xóa…' : '🗑 Xóa sách'}
              </button>
            </div>
            {exportError && <p className="muted error-text">✗ {exportError}</p>}
          </div>
        </div>

        {book.synopsis && <p className="synopsis">{chapterHtmlToText(book.synopsis)}</p>}

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
                  <>
                    <div className="toc-folder-actions">
                      <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={translatingGroup !== null}
                        onClick={() => void translateTitles(group)}
                      >
                        {translatingGroup === group.start ? 'Đang dịch title…' : 'Dịch title mục này'}
                      </button>
                      {titleError && translatingGroup === null && (
                        <span className="error-text" role="alert">{titleError}</span>
                      )}
                    </div>
                    <div className="toc-list">
                      {group.chapters.map((ch) => (
                        <div key={ch.id} className={`toc-item${ch.saved ? ' saved' : ' missing'}`}>
                          <button
                            type="button"
                            className="toc-item-open"
                            disabled={!ch.saved}
                            title={ch.saved ? 'Open chapter' : 'Not downloaded yet — use Fetch missing'}
                            onClick={() => void openChapter(ch.id)}
                          >
                            <span className="toc-dot">{ch.saved ? '✓' : '·'}</span>
                            <span className="toc-num">{ch.id}.</span>
                            <span className="toc-title">{ch.title}</span>
                          </button>
                          <button
                            type="button"
                            className="btn btn-ghost toc-copy"
                            disabled={!ch.saved || copyingChapterId !== null}
                            aria-label={`Copy chapter ${ch.id}`}
                            title={copyError?.chapterId === ch.id ? copyError.message : 'Copy chapter text'}
                            onClick={() => void copyChapter(ch)}
                          >
                            {copyingChapterId === ch.id ? 'Copying…' : copiedChapterId === ch.id ? '✓ Copied' : '⧉ Copy'}
                          </button>
                          {copyError?.chapterId === ch.id && (
                            <span className="error-text toc-copy-error" role="alert">Copy failed</span>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>

        {exportDialog && (
          <div
            className="settings-overlay"
            ref={exportDialogRef}
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-label="Export options"
            onKeyDown={(e) => {
              if (e.key === 'Escape') closeExportDialog();
            }}
          >
            <form className="settings-modal export-dialog" onSubmit={submitExport}>
              <div className="settings-head">
                <h3>📦 Export {exportDialog.toUpperCase()}</h3>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={closeExportDialog}
                  aria-label="Đóng"
                >
                  ✕
                </button>
              </div>
              <section className="settings-group">
                <label className="settings-field">
                  <span>Số chương trong mỗi file</span>
                  <input
                    type="number"
                    min={1}
                    max={MAX_EXPORT_PER_FILE}
                    step={1}
                    value={perFileInput}
                    data-dialog-initial-focus
                    onChange={(e) => {
                      setPerFileInput(e.target.value);
                      setDialogError('');
                    }}
                  />
                  <small>
                    {estimatedFiles > 0
                      ? `${book.saved_count} chương đã lưu → khoảng ${estimatedFiles} file.`
                      : `Mặc định ${DEFAULT_EXPORT_PER_FILE} chương/file.`}
                  </small>
                  <small>
                    Thông tin truyện (tên, tác giả, giới thiệu, nguồn, tags, bìa) chỉ có trong
                    file đầu tiên — file chứa chương 1.
                  </small>
                </label>
              </section>
              {needsChapterNumbers && (
                <fieldset className="settings-group export-number-choice">
                  <legend>Bạn có muốn lưu số chương vào file xuất không?</legend>
                  <label className="settings-check">
                    <input type="radio" name="include-chapter-number" checked={includeChapterNumber === true}
                      onChange={() => { setIncludeChapterNumber(true); setDialogError(''); }} />
                    Có — chỉ thêm cho chương chưa có số
                  </label>
                  <label className="settings-check">
                    <input type="radio" name="include-chapter-number" checked={includeChapterNumber === false}
                      onChange={() => { setIncludeChapterNumber(false); setDialogError(''); }} />
                    Không — giữ nguyên nội dung chương
                  </label>
                </fieldset>
              )}
              {dialogError && (
                <p className="muted error-text" role="alert">
                  ✗ {dialogError}
                </p>
              )}
              <div className="settings-actions">
                <button type="button" className="btn btn-ghost" onClick={closeExportDialog}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={exporting !== null}>
                  Export {exportDialog.toUpperCase()}
                </button>
              </div>
            </form>
          </div>
        )}

        {reading && (
          <div ref={readerDialogRef} className="reader-overlay" role="dialog" aria-modal="true" aria-label="Chapter reader" tabIndex={-1}>
            <div className="reader-panel">
              <div className="reader-head">
                <h3>
                  #{reading.id} — {showingTranslation ? translation?.translated?.title : reading.title}
                </h3>
                <div className="reader-head-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={copyingChapterId !== null}
                    title={copyError?.chapterId === reading.id ? copyError.message : 'Copy visible chapter text'}
                    onClick={() => void copyReaderChapter()}
                  >
                    {copyingChapterId === reading.id ? 'Copying…' : copiedChapterId === reading.id ? '✓ Copied' : '⧉ Copy'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={deletingChapter || crawlMode !== null}
                    onClick={() => void deleteChapter(reading)}
                  >
                    {deletingChapter ? 'Đang xóa…' : '🗑 Xóa chương'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    title="Close reader (Esc)"
                    aria-keyshortcuts="Escape"
                    onClick={closeReader}
                  >
                    ✕ Close (Esc)
                  </button>
                </div>
              </div>
              {copyError?.chapterId === reading.id && (
                <p className="error-text reader-copy-error" role="alert">Copy failed: {copyError.message}</p>
              )}
              <div className="reader-controls">
                <div className="reader-controls-row">
                  <label className="reader-import">
                    <span>Import dictionary cho truyện (.json)</span>
                    <input type="file" accept=".json,application/json"
                      disabled={readerBusy || activeChapter !== null || !dictionaryInfo}
                      onChange={(event) => void importDictionary(event)} />
                  </label>
                  <span className="muted">
                    {dictionaryInfo ? `${translation?.entry_count ?? dictionaryInfo.entry_count} thuật ngữ` : 'Đang tải dictionary…'}
                    {dictionaryInfo?.imported ? ' · Đã import (file mới sẽ thay thế)' : ''}
                  </span>
                </div>
                <div className="reader-controls-row">
                  <button type="button" className="btn btn-primary"
                    disabled={readerBusy || !translation || activeChapter !== null}
                    onClick={() => void translateReaderChapter()}>
                    {readerBusy ? 'Đang xử lý…' : translation?.translated ? 'Dịch lại' : 'Dịch chương'}
                  </button>
                  <div className="reader-view-switch" role="group" aria-label="Chế độ đọc">
                    <button type="button" className={`btn ${!showingTranslation ? 'btn-primary' : 'btn-secondary'}`}
                      aria-pressed={!showingTranslation} onClick={() => setReaderView('raw')}>RAW</button>
                    <button type="button" className={`btn ${showingTranslation ? 'btn-primary' : 'btn-secondary'}`}
                      aria-pressed={showingTranslation}
                      disabled={!translation?.translated || translation.stale || activeChapter === reading.id}
                      onClick={() => setReaderView('translated')}>Dịch</button>
                  </div>
                </div>
                {activeChapter !== null && activeChapter !== reading.id &&
                  <p className="reader-status">Đang xử lý chương {activeChapter}. Hoàn tất hoặc hủy job đó trước khi dịch/import.</p>}
                {translation?.stale && <p className="reader-status">
                  Bản dịch cũ không còn khớp RAW hoặc thuật ngữ hiện tại. Đang hiển thị RAW; chọn Dịch lại để cập nhật.
                </p>}
                {translation?.job && <div className="reader-job-status">
                  <span role="status">Dịch chương: {translation.job.status} · {translation.job.stage_label ?? translation.job.stage ?? 'Đang chuẩn bị'}</span>
                  {['pending', 'running', 'failed', 'interrupted'].includes(translation.job.status) &&
                    <button type="button" className="btn btn-ghost" disabled={readerBusy}
                      onClick={() => void readerJobAction('cancel')}>Hủy job</button>}
                  {['failed', 'interrupted'].includes(translation.job.status) && !translation.job.manual_review &&
                    <button type="button" className="btn btn-primary" disabled={readerBusy}
                      onClick={() => void readerJobAction('resume')}>Tiếp tục</button>}
                </div>}
                {(readerInfoError || translation?.error || translation?.job?.error) &&
                  <p className="error-text" role="alert">
                    {readerInfoError || translation?.error || translation?.job?.error}
                    {translation?.job?.error_detail ? ` · ${readerError(translation.job.error_detail)}` : ''}
                  </p>}
                {readerInfoError && !translation &&
                  <div className="reader-controls-row">
                    <button type="button" className="btn btn-ghost"
                      onClick={() => void loadReaderInfo(reading.id, readerEpoch.current)}>
                      Tải lại trạng thái dịch
                    </button>
                    <button type="button" className="btn btn-secondary" disabled={readerBusy}
                      onClick={() => void translateReaderChapter(true)}>
                      Thử dịch lại
                    </button>
                  </div>}
                {translation?.job?.manual_review && ['failed', 'interrupted'].includes(translation.job.status) &&
                  <section className="reader-review" aria-label="Duyệt đoạn dịch thủ công">
                    <h4>Duyệt chương {translation.job.manual_review.chapter} · {translation.job.manual_review.paragraph_id}</h4>
                    <p>RAW</p>
                    <p className="reader-review-text">{translation.job.manual_review.raw}</p>
                    <p>Phát hiện cần xử lý</p>
                    <pre>{JSON.stringify(translation.job.manual_review.findings, null, 2)}</pre>
                    <label htmlFor="reader-review-text">Bản dịch hiện tại (sửa toàn đoạn hoặc chấp nhận giữ nguyên)</label>
                    <textarea id="reader-review-text" key={translation.job.manual_review.fingerprint}
                      ref={reviewTextRef} defaultValue={translation.job.manual_review.current_text} rows={5} disabled={readerBusy} />
                    <div className="reader-controls-row">
                      <button type="button" className="btn btn-primary" disabled={readerBusy}
                        onClick={() => void readerJobAction('review', 'replace', reviewTextRef.current?.value ?? '')}>
                        Lưu bản sửa và tiếp tục
                      </button>
                      <button type="button" className="btn btn-secondary" disabled={readerBusy}
                        onClick={() => void readerJobAction('review', 'accept')}>
                        Giữ nguyên và tiếp tục
                      </button>
                    </div>
                  </section>}
              </div>
              {showingTranslation
                ? <div className="reader-body reader-translated">
                    {translation?.translated?.paragraphs.map((paragraph, index) =>
                      <p key={index}>{paragraph}</p>)}
                  </div>
                : /* Chapter bodies come from our crawler pipeline (sanitized by the source cleaner). */
                  <div className="reader-body" dangerouslySetInnerHTML={{ __html: reading.body }} />}
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
