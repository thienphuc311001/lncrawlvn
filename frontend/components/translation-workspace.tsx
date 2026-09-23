'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

const API = '/api/translation';
type TranslationEvent = {
  id?: string; timestamp?: string; task?: string; model?: string; actual_model?: string; event?: string; status: string;
  attempt?: number; key_slot?: number; retry_after?: number; category?: string; operation?: string; reason?: string; error?: string; message?: string;
  key_rotated?: boolean; model_changed?: boolean; model_fallback_started?: boolean;
  source?: string; occurrences?: number; confidence?: number; resolver_attempts?: number;
  fallback?: string; severity?: string; continue?: boolean;
  findings?: Array<Record<string, unknown>>; resolved_findings?: Array<Record<string, unknown>>;
  remaining_findings?: Array<Record<string, unknown>>; new_findings?: Array<Record<string, unknown>>;
};
type ManualReview = {
  chapter: number; paragraph_id: string; raw: string; current_text: string;
  findings: Array<Record<string, unknown>>; fingerprint: string;
};
type Job = {
  job_id: string; status: string; stage?: string; error?: string; error_detail?: unknown;
  stage_label?: string; display_title?: string; book_title?: string; chapter_start?: number; chapter_end?: number;
  total_chapters?: number; completed_chapters?: number; chapters_finalized?: number;
  current_chapter?: string | number | null;
  confirmed_terms?: number; candidate_count?: number;
  logs?: TranslationEvent[];
  dictionary_hash?: string;
  dictionary_report?: { confirmed_terms?: number; unresolved_terms?: number };
  unresolved_terms?: number;
  outputs?: { translation?: string; dictionary?: string; unresolved?: string; author_notes?: string };
  output_filenames?: { translation?: string; dictionary?: string; unresolved?: string; author_notes?: string };
  author_note_policy?: string;
  request_warning?: { code: string; severity: string; logical_calls: number };
  manual_review?: ManualReview;
  request_statistics?: {
    total_requests: number; requests: Record<string, number>; logical_operations: Record<string, number>;
    local_ai_requests: Record<string, number>; retry: number; model_fallback: number; cache_hits: number;
    logical_call_count?: number; api_attempt_count?: number; technical_retry_count?: number;
    fallback_count?: number; cache_hit_count?: number;
    by_operation?: Record<string, { logical_calls: number; api_attempts: number;
      technical_retries: number; fallbacks: number; cache_hits: number }>;
  };
};
type Config = { models: Record<string, string>; concurrency: number; stagger_ms: number; api_key_configured: boolean; api_key_count?: number; author_note_policy?: string };

const ERROR_LABELS: Record<string, string> = {
  error: 'Error', severity: 'Severity', input: 'Input', line: 'Line', heading: 'Heading',
  volume: 'Volume', chapter: 'Chapter', previous_chapter: 'Previous chapter',
  previous_line: 'Previous line', counterpart: 'Counterpart', reason: 'Reason', message: 'Message',
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function readableError(value: unknown): ReactNode {
  if (value === null || value === undefined) return 'Not available';
  if (Array.isArray(value)) return <ul>{value.map((item, index) => <li key={index}>{readableError(item)}</li>)}</ul>;
  if (isRecord(value)) {
    // FastAPI/Pydantic request validation uses an array of {loc, msg, type, ...}.
    if (typeof value.msg === 'string') {
      const location = Array.isArray(value.loc) ? value.loc.map(String).join(' → ') : '';
      return <span>{location && <strong>{location}: </strong>}{value.msg}</span>;
    }
    const fields = Object.entries(value).filter(([, field]) => field !== undefined);
    return fields.length ? <dl>{fields.map(([key, field]) => <div key={key}>
      <dt><strong>{ERROR_LABELS[key] ?? key.replaceAll('_', ' ')}</strong></dt>
      <dd>{readableError(field)}</dd>
    </div>)}</dl> : 'An unexpected error occurred.';
  }
  return String(value);
}

function ErrorNotice({ detail }: { detail: unknown }) {
  if (detail === null || detail === undefined || detail === '') return null;
  return <div role="alert" className="error-text">{readableError(detail)}</div>;
}

function translationLogText(event: TranslationEvent): string {
  const metadata = [
    (event.event ?? event.status).toUpperCase(), event.task, event.operation?.replaceAll('_', ' '), event.source,
    event.actual_model ?? event.model,
    event.key_slot !== undefined ? `Key slot ${event.key_slot}` : null,
    event.category, event.attempt !== undefined ? `Attempt ${event.attempt}` : null,
    event.retry_after !== undefined ? `Retry after ${event.retry_after}s` : null,
    event.confidence !== undefined ? `Confidence ${(event.confidence * 100).toFixed(1)}%` : null,
    event.fallback ? `Fallback ${event.fallback}` : null,
    event.severity, event.continue === false ? 'Continue false' : null,
  ].filter(Boolean).join(' · ');
  const timestamp = event.timestamp ? `[${event.timestamp.replace('T', ' ').replace(/\.\d+/, '')}] ` : '';
  const message = event.error ?? event.message ?? event.reason;
  return `${timestamp}${metadata}${message ? ` — ${message}` : ''}`;
}

function translationLogLevel(event: TranslationEvent): string {
  if (event.status === 'failed' || event.status === 'error') return 'error';
  if (['retrying', 'fallback', 'key_rotation', 'rate_limit_cooldown', 'waiting_for_rate_limit', 'cancelled', 'interrupted'].includes(event.status)) return 'warning';
  return '';
}

function TranslationLog({ logs = [], status }: { logs?: TranslationEvent[]; status: string }) {
  const [open, setOpen] = useState(true);
  const [copied, setCopied] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bodyId = useId();
  const running = status === 'pending' || status === 'running';
  const consoleStatus = running ? 'running' : status === 'done' ? 'done' : status === 'failed' ? 'failed' : 'idle';
  const visible = logs.slice(-100);
  const logText = visible.map(translationLogText).join('\n');

  useEffect(() => {
    const box = boxRef.current;
    if (box && open) box.scrollTop = box.scrollHeight;
  }, [logText, open]);

  useEffect(() => () => {
    if (copyTimer.current !== null) clearTimeout(copyTimer.current);
  }, []);

  async function copyLog() {
    try {
      await navigator.clipboard.writeText(logText);
      setCopied(true);
      if (copyTimer.current !== null) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return <section className={`job-console translation-log${open ? ' open' : ''}`} aria-label="Translation activity">
    <header className="job-console-head">
      <button type="button" className="job-console-title" onClick={() => setOpen(value => !value)}
        aria-expanded={open} aria-controls={bodyId} title={open ? 'Thu gọn' : 'Mở rộng'}>
        <span className={`jc-dot ${consoleStatus}`} aria-hidden="true" />
        <span className={`jc-status ${consoleStatus}`}>{status.toUpperCase()}</span>
        <span className="jc-name">Translation activity</span>
        <span className="jc-meta" title="Latest 100 events, oldest first">{visible.length} dòng</span>
      </button>
      <div className="job-console-actions">
        <button type="button" className="btn btn-ghost" disabled={!visible.length} onClick={() => void copyLog()}>
          {copied ? '✓ Đã copy' : '⧉ Copy'}
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => setOpen(value => !value)}
          aria-label={open ? 'Thu gọn translation console' : 'Mở rộng translation console'} aria-expanded={open} aria-controls={bodyId}>
          {open ? '▾' : '▴'}
        </button>
      </div>
    </header>
    {open && <div id={bodyId} className="job-console-body" role="log" aria-label="Translation events"
      aria-live="polite" aria-relevant="additions" tabIndex={0} ref={boxRef}>
      {!visible.length && <span className="line muted">No activity recorded yet.</span>}
      {visible.map((event, index) => <span key={event.id ?? `${event.timestamp ?? ''}-${index}`}
        className={`line line-in ${translationLogLevel(event)}`}>{translationLogText(event)}</span>)}
      {running && <span className="line"><span className="job-cursor" aria-hidden="true" /></span>}
    </div>}
  </section>;
}

class RequestError extends Error {
  constructor(readonly detail: unknown, status: number) {
    super(typeof detail === 'string' ? detail : `Request failed (HTTP ${status}).`);
  }
}

function errorDetail(error: unknown): unknown {
  return error instanceof RequestError ? error.detail : error instanceof Error ? error.message : String(error);
}

function chapterLabel(key: string): string {
  const scoped = /^v(\d+)-c(\d+)$/.exec(key);
  return scoped ? `Volume ${scoped[1]} / Chapter ${scoped[2]}` : `Chapter ${key}`;
}

function readableChapter(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '';
  return chapterLabel(String(value));
}

function statusLabel(status: string): string {
  return ({ pending: 'QUEUED', running: 'RUNNING', done: 'COMPLETED', failed: 'FAILED', cancelled: 'CANCELLED', interrupted: 'PAUSED', deleting: 'DELETING' } as Record<string, string>)[status] ?? status.toUpperCase();
}

async function request(path: string, body?: unknown, method?: 'POST' | 'DELETE'): Promise<unknown> {
  const res = await fetch(`${API}${path}`, body === undefined && !method ? {} : {
    method: method ?? 'POST', headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await res.json();
  if (!res.ok) throw new RequestError(data?.detail ?? `Request failed (HTTP ${res.status}).`, res.status);
  return data;
}

export default function TranslationWorkspace() {
  const [files, setFiles] = useState<Record<string, File | undefined>>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const reviewTextRef = useRef<HTMLTextAreaElement | null>(null);
  const active = job?.status === 'pending' || job?.status === 'running';

  useEffect(() => {
    let live = true;
    Promise.all([request('/config'), request('/jobs')]).then(([configuration, history]) => {
      if (!live) return;
      setConfig(configuration as Config);
      setJobs(history as Job[]);
      setJob((history as Job[])[0] ?? null);
      const selected = (history as Job[])[0];
      if (selected) request(`/jobs/${selected.job_id}`).then(data => {
        if (live) setJob(data as Job);
      }).catch(e => { if (live) setError(errorDetail(e)); });
    }).catch(e => { if (live) setError(errorDetail(e)); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!job || !active) return;
    let live = true;
    const timer = setInterval(() => {
      request(`/jobs/${job.job_id}`).then(data => {
        if (live) setJob(data as Job);
      }).catch(e => { if (live) setError(errorDetail(e)); });
    }, 1500);
    return () => { live = false; clearInterval(timer); };
  }, [job?.job_id, active]);

  async function start() {
    setBusy(true); setError(null);
    try {
      if (!files.raw) throw new Error('Select a RAW Chinese text file.');
      const [raw, dictionary] = await Promise.all([
        files.raw.text(), files.dictionary?.text(),
      ]);
      const result = await request('/jobs', {
        raw,
        dictionary: dictionary ? JSON.parse(dictionary) : null,
        book_title: files.raw.name.replace(/\.[^.]+$/, ''),
        source_name: files.raw.name,
      }) as Job;
      setJob(result);
      setJobs(previous => [result, ...previous.filter(j => j.job_id !== result.job_id)]);
    } catch (e) { setError(errorDetail(e)); }
    finally { setBusy(false); }
  }

  async function deleteCurrent() {
    if (!job) return;
    const running = active;
    const title = job.display_title ?? `Translation batch ${job.job_id.slice(0, 8)}`;
    const prompt = running
      ? `Delete running batch?\n\n${title}\n\nThe active translation will be cancelled and the batch removed. Completed output files, if any, will be kept.`
      : `Delete ${statusLabel(job.status).toLowerCase()} batch?\n\n${title}\n\nBatch history and temporary state will be removed. Generated outputs will be kept.`;
    if (typeof window !== 'undefined' && !window.confirm(prompt)) return;
    setBusy(true); setError(null);
    try {
      await request(`/jobs/${job.job_id}`, undefined, 'DELETE');
      const remaining = jobs.filter(item => item.job_id !== job.job_id);
      setJobs(remaining);
      setJob(remaining[0] ?? null);
    } catch (e) { setError(errorDetail(e)); }
    finally { setBusy(false); }
  }

  async function action(name: 'cancel' | 'resume') {
    if (!job) return;
    setBusy(true); setError(null);
    try { setJob(await request(`/jobs/${job.job_id}/${name}`, {}) as Job); }
    catch (e) { setError(errorDetail(e)); }
    finally { setBusy(false); }
  }

  async function resolveReview(decision: 'accept' | 'replace') {
    if (!job?.manual_review) return;
    const review = job.manual_review;
    if (decision === 'accept' && typeof window !== 'undefined' && !window.confirm(
      'Giữ nguyên đoạn dịch dù còn lỗi được liệt kê? Quyết định này sẽ được ghi lại; không xóa đoạn văn.'
    )) return;
    setBusy(true); setError(null);
    try {
      setJob(await request(`/jobs/${job.job_id}/review`, {
        chapter: review.chapter, paragraph_id: review.paragraph_id,
        fingerprint: review.fingerprint, action: decision,
        ...(decision === 'replace' ? { text: reviewTextRef.current?.value ?? '' } : {}),
      }) as Job);
    } catch (e) { setError(errorDetail(e)); }
    finally { setBusy(false); }
  }

  return <section className="container translation-workspace">
    <h1>Novel translation</h1>
    <p className="muted">Chinese → Vietnamese. RAW supplies meaning; your cumulative book dictionary locks terminology.</p>
    <div className="translation-inputs">
      {(['raw', 'dictionary'] as const).map(key => <label key={key} className="settings-field">
        <span>{key.toUpperCase()}{key === 'dictionary' ? ' (optional)' : ''}</span>
        <input type="file" accept={key === 'dictionary' ? '.json' : '.txt'} disabled={busy || active}
          onChange={e => setFiles(previous => ({ ...previous, [key]: e.target.files?.[0] }))} />
      </label>)}
    </div>
    <p className="muted">UTF-8 RAW text with numbered 第N章 chapter headings. Gaps are allowed; duplicate or backwards headings are errors. The optional dictionary is cumulative JSON.</p>
    <button className="btn btn-primary" disabled={busy || active || !config?.api_key_configured || !files.raw} onClick={() => void start()}>
      {busy ? 'Working…' : 'Translate batch'}
    </button>
    {config && <details className="translation-config"><summary>System Configuration</summary>
      <p>Requests prefer the primary model across configured keys, then the fallback model. Daily quota disables only that key/model pair for this run. RPM/TPM limits suspend that pair for about 60 seconds. Temporary errors get two retries per pair by default.</p>
      {Object.entries(config.models).map(([role, model]) => <label className="settings-field" key={role}>
        <span>{role[0].toUpperCase() + role.slice(1)} Model</span><input readOnly value={model} />
      </label>)}
    </details>}
    {config && <p className="muted">{config.concurrency} workers · {config.stagger_ms} ms staggering · Author notes: {config.author_note_policy ?? 'preserve'} · Server API key {config.api_key_configured ? 'configured' : 'missing — set GOOGLE_AI_API_KEY on the server'}</p>}
    {config?.api_key_configured && <p className="muted">{config.api_key_count ?? 1} API key(s) configured · Automatic rotation on quota errors · Key values stay on the server</p>}
    {jobs.length > 0 && <label className="settings-field"><span>Saved batches</span><select value={job?.job_id ?? ''} disabled={busy}
      onChange={e => { request(`/jobs/${e.target.value}`).then(data => setJob(data as Job)).catch(e => setError(errorDetail(e))); }}>
      {jobs.map(j => <option key={j.job_id} value={j.job_id}>{j.display_title ?? `Translation batch ${j.job_id.slice(0, 8)}`}</option>)}
    </select></label>}
    {job && <div className="translation-progress" aria-live="polite">
      <h2>{job.display_title ?? `Translation batch ${job.job_id.slice(0, 8)}`}</h2>
      <p>{statusLabel(job.status)} · {job.stage_label ?? job.stage ?? 'Preparing inputs'}</p>
      <p className="muted">Batch ID: {job.job_id}</p>
      <p>{job.chapters_finalized ?? job.completed_chapters ?? 0}/{job.total_chapters ?? '?'} chapters finalized</p>
      {(job.current_chapter !== undefined && job.current_chapter !== null) && <p>Currently working: {readableChapter(job.current_chapter)}</p>}
      {job.dictionary_hash && <p>Dictionary hash: {job.dictionary_hash.slice(0, 12)}</p>}
      {(job.confirmed_terms !== undefined || job.unresolved_terms !== undefined) && <p>Terminology: {job.confirmed_terms ?? 0} confirmed · {job.unresolved_terms ?? 0} unresolved</p>}
      {job.dictionary_report && <details className="dictionary-report">
        <summary>Dictionary summary</summary>
        <p>{job.dictionary_report.confirmed_terms ?? 0} confirmed · {job.dictionary_report.unresolved_terms ?? 0} unresolved</p>
      </details>}
      {job.candidate_count !== undefined && <p>Terminology candidates: {job.candidate_count}</p>}
      {job.request_warning && <p role="alert" className="error-text">Request budget anomaly: {job.request_warning.logical_calls} logical calls ({job.request_warning.severity}). Review the operation breakdown below.</p>}
      <ErrorNotice detail={job.error_detail ?? job.error} />
      {job.status === 'failed' && job.manual_review && <section className="translation-manual-review" aria-label="Manual translation review">
        <h3>Duyệt đoạn dịch thủ công · Chương {job.manual_review.chapter} · {job.manual_review.paragraph_id}</h3>
        <p>RAW tiếng Trung</p>
        <p className="translation-review-raw">{job.manual_review.raw}</p>
        <p>Lỗi cần quyết định</p>
        <ErrorNotice detail={job.manual_review.findings} />
        <label className="settings-field" htmlFor="translation-review-text"><span>Bản dịch tiếng Việt</span></label>
        <textarea id="translation-review-text" key={job.manual_review.fingerprint}
          ref={reviewTextRef} defaultValue={job.manual_review.current_text} rows={6} disabled={busy} />
        <div className="translation-review-actions">
          <button className="btn btn-primary" disabled={busy} onClick={() => void resolveReview('replace')}>Lưu bản sửa và tiếp tục</button>
          <button className="btn btn-ghost" disabled={busy} onClick={() => void resolveReview('accept')}>Chấp nhận bản hiện tại và tiếp tục</button>
        </div>
        <p className="muted">“Chấp nhận bản hiện tại” chỉ bỏ qua lỗi đã duyệt ở đoạn này và được ghi nhận là manual override. Không có thao tác xóa/bỏ qua cả đoạn văn.</p>
      </section>}
      {active ? <button className="btn btn-ghost" disabled={busy} onClick={() => void action('cancel')}>Cancel</button>
        : job.status !== 'done' && !job.manual_review && <button className="btn btn-primary" disabled={busy} onClick={() => void action('resume')}>Resume batch</button>}
      {job.status === 'done' && <div className="translation-downloads">
        <a className="btn btn-primary" href={job.outputs?.translation ?? `${API}/jobs/${job.job_id}/outputs/translated.txt`}
          download={job.output_filenames?.translation}>{job.output_filenames?.translation ?? 'Translation TXT'}</a>
        <a className="btn btn-primary" href={job.outputs?.dictionary ?? `${API}/jobs/${job.job_id}/outputs/dictionary.json`}
          download={job.output_filenames?.dictionary}>{job.output_filenames?.dictionary ?? 'Full merged dictionary JSON'}</a>
        <a className="btn btn-primary" href={job.outputs?.unresolved ?? `${API}/jobs/${job.job_id}/outputs/unresolved.json`}
          download={job.output_filenames?.unresolved}>{job.output_filenames?.unresolved ?? 'Unresolved terms JSON'}</a>
        {job.outputs?.author_notes && <a className="btn btn-primary" href={job.outputs.author_notes}
          download={job.output_filenames?.author_notes}>{job.output_filenames?.author_notes ?? 'Author notes TXT'}</a>}
      </div>}
      <button className="btn btn-ghost" disabled={busy} onClick={() => void deleteCurrent()}>Delete batch</button>
      <TranslationLog key={job.job_id} logs={job.logs} status={job.status} />
      {job.request_statistics && <details className="translation-statistics"><summary>Request statistics · {job.request_statistics.api_attempt_count ?? job.request_statistics.total_requests} API attempts</summary>
        <table><thead><tr><th>Operation</th><th>Logical calls</th><th>API attempts</th><th>Technical retries</th><th>Fallbacks</th><th>Cache hits</th></tr></thead><tbody>
          {Object.entries(job.request_statistics.by_operation ?? {}).map(([operation, count]) => <tr key={operation}>
            <td>{operation.replaceAll('_', ' ')}</td><td>{count.logical_calls}</td><td>{count.api_attempts}</td><td>{count.technical_retries}</td><td>{count.fallbacks}</td><td>{count.cache_hits}</td>
          </tr>)}
          {!job.request_statistics.by_operation && Object.entries(job.request_statistics.requests).map(([operation, count]) => <tr key={operation}>
            <td>{operation.replaceAll('_', ' ')}</td><td>{job.request_statistics?.logical_operations[operation] ?? 0}</td><td>{count}</td><td>—</td><td>—</td><td>—</td>
          </tr>)}
          {Object.entries(job.request_statistics.local_ai_requests).map(([operation, count]) => <tr key={operation}>
            <td>{operation.replaceAll('_', ' ')} (local)</td><td>0</td><td>{count}</td><td>—</td><td>—</td><td>—</td>
          </tr>)}
        </tbody></table>
        <p>Technical retries: {job.request_statistics.technical_retry_count ?? job.request_statistics.retry} · Model fallbacks: {job.request_statistics.fallback_count ?? job.request_statistics.model_fallback} · Cached AI results reused: {job.request_statistics.cache_hit_count ?? job.request_statistics.cache_hits}</p>
      </details>}
    </div>}
    <ErrorNotice detail={error} />
  </section>;
}
