'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

const API = 'http://127.0.0.1:8000/api/translation';
type TranslationEvent = {
  id?: string; timestamp?: string; task?: string; model?: string; status: string;
  attempt?: number; key_slot?: number; retry_after?: number; category?: string; operation?: string; reason?: string; error?: string; message?: string;
  source?: string; occurrences?: number; confidence?: number; resolver_attempts?: number;
  fallback?: string; severity?: string; continue?: boolean;
  findings?: Array<Record<string, unknown>>; resolved_findings?: Array<Record<string, unknown>>;
  remaining_findings?: Array<Record<string, unknown>>; new_findings?: Array<Record<string, unknown>>;
};
type Job = {
  job_id: string; status: string; stage?: string; error?: string; error_detail?: unknown;
  total_chapters?: number; completed_chapters?: number; aligned?: number;
  scanned_chapter?: number;
  candidate_count?: number; resolved_candidates?: number; current_term?: string;
  active_chapters?: Record<string, { chunk: number; chunks: number }>;
  logs?: TranslationEvent[];
  dictionary_hash?: string; dictionary_frozen?: boolean;
  dictionary_report?: {
    confirmed_terms?: number; ignored_candidates?: number;
    locked_terms?: number; provisional_terms?: number; needs_review?: number;
    report_only_terms?: number; unresolved_terms: number; unresolved_plausible_terms?: number;
    rejected_generic_candidates?: number; fatal_conflicts?: number;
    removed_contextual_forms?: number; preserved_identity_forms?: number;
    register?: string; register_source?: string;
    register_normalizations?: number; register_conflicts?: number;
  };
  dictionary_review?: Array<{
    source: string; translation?: string | null; reason?: string; severity?: string;
    state?: string; resolution_status?: string; confidence?: number; resolver_attempts?: number;
    fallback?: string; continue?: boolean;
  }>;
  request_statistics?: {
    total_requests: number; requests: Record<string, number>; logical_operations: Record<string, number>;
    local_ai_requests: Record<string, number>; retry: number; model_fallback: number; cache_hits: number;
  };
};
type Config = { models: Record<string, string>; concurrency: number; stagger_ms: number; api_key_configured: boolean; api_key_count?: number };

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
    event.status.toUpperCase(), event.task, event.operation?.replaceAll('_', ' '), event.source, event.model,
    event.key_slot !== undefined ? `Key slot ${event.key_slot}` : null,
    event.category, event.attempt !== undefined ? `Attempt ${event.attempt}/2` : null,
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

async function request(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`${API}${path}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
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
      if (!files.raw || !files.vietphrase) throw new Error('Select RAW and VIETPHRASE text files.');
      const [raw, vietphrase, dictionary] = await Promise.all([
        files.raw.text(), files.vietphrase.text(), files.dictionary?.text(),
      ]);
      const result = await request('/jobs', { raw, vietphrase, dictionary: dictionary ? JSON.parse(dictionary) : null }) as Job;
      setJob(result);
      setJobs(previous => [result, ...previous.filter(j => j.job_id !== result.job_id)]);
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

  return <section className="container translation-workspace">
    <h1>Novel translation</h1>
    <p className="muted">Chinese → Vietnamese. RAW supplies meaning; VietPhrase supplies readings; your book dictionary keeps terminology consistent.</p>
    <div className="translation-inputs">
      {(['raw', 'vietphrase', 'dictionary'] as const).map(key => <label key={key} className="settings-field">
        <span>{key.toUpperCase()}{key === 'dictionary' ? ' (optional)' : ''}</span>
        <input type="file" accept={key === 'dictionary' ? '.json' : '.txt'} disabled={busy || active}
          onChange={e => setFiles(previous => ({ ...previous, [key]: e.target.files?.[0] }))} />
      </label>)}
    </div>
    <p className="muted">UTF-8 text with matching numbered chapter headings (第1章 / Chương 1 / Chapter 1). Numbered volumes may scope chapters; volume and chapter order must match in both files. Gaps are allowed. Duplicate headings and tables of contents are accepted only when clearly redundant, never by silently discarding chapter content. The dictionary is cumulative JSON.</p>
    <button className="btn btn-primary" disabled={busy || active || !config?.api_key_configured || !files.raw || !files.vietphrase} onClick={() => void start()}>
      {busy ? 'Working…' : 'Translate batch'}
    </button>
    {config && <details className="translation-config"><summary>System Configuration</summary>
      <p>Requests prefer Primary, then the other configured keys and fallback models. Daily quota disables only that key/model for this run. RPM/TPM limits suspend that pair for 60 seconds while other pairs continue. Temporary errors get up to two attempts per pair. Authentication or configuration errors disable the affected key; processing continues while a usable pair remains.</p>
      {Object.entries(config.models).map(([role, model]) => <label className="settings-field" key={role}>
        <span>{role[0].toUpperCase() + role.slice(1)} Model</span><input readOnly value={model} />
      </label>)}
    </details>}
    {config && <p className="muted">{config.concurrency} workers · {config.stagger_ms} ms staggering · Server API key {config.api_key_configured ? 'configured' : 'missing — set GOOGLE_AI_API_KEY on the server'}</p>}
    {config?.api_key_configured && <p className="muted">{config.api_key_count ?? 1} API key(s) configured · Automatic rotation on quota errors · Key values stay on the server</p>}
    {jobs.length > 0 && <label className="settings-field"><span>Saved batches</span><select value={job?.job_id ?? ''} disabled={busy}
      onChange={e => { request(`/jobs/${e.target.value}`).then(data => setJob(data as Job)).catch(e => setError(errorDetail(e))); }}>
      {jobs.map(j => <option key={j.job_id} value={j.job_id}>{j.job_id.slice(0, 12)}</option>)}
    </select></label>}
    {job && <div className="translation-progress" aria-live="polite">
      <h2>{job.stage ?? job.status}</h2>
      <p>{job.status} · {job.completed_chapters ?? 0}/{job.total_chapters ?? '?'} chapters finalized</p>
      {job.aligned !== undefined && <p>Aligned chapters: {job.aligned}</p>}
      {job.dictionary_frozen && <p>Dictionary frozen for this batch · {job.dictionary_hash?.slice(0, 12)}</p>}
      {job.dictionary_report && <details className="dictionary-report" open={(job.dictionary_report.ignored_candidates ?? job.dictionary_report.report_only_terms ?? job.dictionary_report.provisional_terms ?? 0) > 0 || (job.dictionary_report.fatal_conflicts ?? 0) > 0}>
        <summary>Dictionary summary · {job.dictionary_report.confirmed_terms ?? job.dictionary_report.locked_terms ?? 0} confirmed · {job.dictionary_report.ignored_candidates ?? job.dictionary_report.report_only_terms ?? job.dictionary_report.provisional_terms ?? 0} ignored</summary>
        <p>Confirmed: {job.dictionary_report.confirmed_terms ?? job.dictionary_report.locked_terms ?? 0} · Ignored candidates: {job.dictionary_report.ignored_candidates ?? job.dictionary_report.report_only_terms ?? job.dictionary_report.provisional_terms ?? 0} · Rejected generic: {job.dictionary_report.rejected_generic_candidates ?? 0} · Removed contextual forms: {job.dictionary_report.removed_contextual_forms ?? 0} · Preserved identity forms: {job.dictionary_report.preserved_identity_forms ?? 0} · Fatal conflicts: {job.dictionary_report.fatal_conflicts ?? 0}</p>
        {job.dictionary_report.register && <p>Address/title register: {job.dictionary_report.register === 'sino-vietnamese' ? 'Sino-Vietnamese' : 'Modern Vietnamese'}{job.dictionary_report.register_source ? ` (${job.dictionary_report.register_source})` : ''} · Normalized forms: {job.dictionary_report.register_normalizations ?? 0} · Inconsistent forms dropped: {job.dictionary_report.register_conflicts ?? 0}</p>}
        {(job.dictionary_review ?? []).map(entry => <article className="dictionary-review-entry" key={entry.source}>
          <strong>{entry.source}</strong>{entry.translation && <span> → {entry.translation}</span>}
          <span> · {entry.state === 'IGNORE' ? 'IGNORED' : entry.severity ?? 'REVIEW_REQUIRED'} · {entry.reason ?? entry.resolution_status ?? (entry.state === 'IGNORE' ? 'Ignored candidate' : 'Review required')}</span>
          {entry.confidence !== undefined && <span> · confidence {(entry.confidence * 100).toFixed(1)}%</span>}
          {entry.resolver_attempts !== undefined && <span> · {entry.resolver_attempts} resolver attempt(s)</span>}
          {entry.fallback && <span> · fallback: {entry.fallback}</span>}
        </article>)}
      </details>}
      {job.stage === 'Dictionary resolution' && job.candidate_count !== undefined && job.resolved_candidates !== undefined && <p>Terms reviewed: {job.resolved_candidates}/{job.candidate_count}{job.current_term ? ` · ${job.current_term}` : ''}</p>}
      {Object.entries(job.active_chapters ?? {}).map(([chapter, value]) => <p key={chapter}>{chapterLabel(chapter)} · Chunk {value.chunk}/{value.chunks}</p>)}
      <ErrorNotice detail={job.error_detail ?? job.error} />
      {active ? <button className="btn btn-ghost" disabled={busy} onClick={() => void action('cancel')}>Cancel</button>
        : job.status !== 'done' && <button className="btn btn-primary" disabled={busy} onClick={() => void action('resume')}>Resume batch</button>}
      {job.status === 'done' && <div className="translation-downloads">
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/translated.txt`}>Vietnamese TXT</a>
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/translated.json`}>Translated Chapters</a>
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/dictionary.json`}>Updated Book Dictionary</a>
      </div>}
      <TranslationLog key={job.job_id} logs={job.logs} status={job.status} />
      {job.request_statistics && <details className="translation-statistics"><summary>Request statistics · {job.request_statistics.total_requests} API requests</summary>
        <table><thead><tr><th>Operation</th><th>Logical calls</th><th>API attempts</th></tr></thead><tbody>
          {Object.entries(job.request_statistics.requests).map(([operation, count]) => <tr key={operation}>
            <td>{operation.replaceAll('_', ' ')}</td><td>{job.request_statistics?.logical_operations[operation] ?? 0}</td><td>{count}</td>
          </tr>)}
          {Object.entries(job.request_statistics.local_ai_requests).map(([operation, count]) => <tr key={operation}>
            <td>{operation.replaceAll('_', ' ')} (local)</td><td>0</td><td>{count}</td>
          </tr>)}
        </tbody></table>
        <p>Retries: {job.request_statistics.retry} · Model fallbacks: {job.request_statistics.model_fallback} · Cached AI results reused: {job.request_statistics.cache_hits}</p>
      </details>}
    </div>}
    <ErrorNotice detail={error} />
  </section>;
}
