'use client';

import { useEffect, useState, type ReactNode } from 'react';

const API = 'http://127.0.0.1:8000/api/translation';
type TranslationEvent = {
  id?: string; timestamp?: string; task?: string; model?: string; status: string;
  attempt?: number; key_slot?: number; retry_after?: number; category?: string; error?: string; message?: string;
};
type Job = {
  job_id: string; status: string; stage?: string; error?: string; error_detail?: unknown;
  total_chapters?: number; completed_chapters?: number; aligned?: number;
  scanned_chapter?: number;
  candidate_count?: number; resolved_candidates?: number; current_term?: string;
  active_chapters?: Record<string, { chunk: number; chunks: number }>;
  logs?: TranslationEvent[];
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

function TranslationLog({ logs = [] }: { logs?: TranslationEvent[] }) {
  return <section className="translation-log" aria-label="Translation activity">
    <h3>Translation activity</h3>
    <p className="muted">Primary → Fallback → Backup. A later model is used only after the preceding model fails. Latest 100 events, newest first.</p>
    {logs.length === 0 ? <p>No activity recorded yet.</p> : <ol role="log" aria-live="polite" aria-relevant="additions" className="translation-log-events">
      {[...logs].reverse().map((event, index) => <li key={event.id ?? `${event.timestamp ?? ''}-${logs.length - index}`}>
        <div className="translation-log-meta">
          {event.timestamp && <time dateTime={event.timestamp}>{event.timestamp.replace('T', ' ').replace(/\.\d+/, '')}</time>}
          <strong className={`translation-log-status translation-log-status-${event.status}`}>{event.status}</strong>
          {event.task && <span>{event.task}</span>}
          {event.model && <span>{event.model}</span>}
          {event.key_slot && <span>Key slot {event.key_slot}</span>}
          {event.category && <span>{event.category}</span>}
          {event.attempt && <span>Attempt {event.attempt}/2</span>}
        </div>
        {(event.error || event.message) && <p>{event.error ?? event.message}</p>}
      </li>)}
    </ol>}
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
      <p>Every uncached task starts with Primary. Quota errors try the next configured API key on the same model before model fallback. Temporary errors get up to two attempts per key; exhausted daily quota skips a retry. Authentication and invalid request errors stop immediately.</p>
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
      {job.stage === 'Dictionary resolution' && job.candidate_count !== undefined && job.resolved_candidates !== undefined && <p>Terms reviewed: {job.resolved_candidates}/{job.candidate_count}{job.current_term ? ` · ${job.current_term}` : ''}</p>}
      {Object.entries(job.active_chapters ?? {}).map(([chapter, value]) => <p key={chapter}>{chapterLabel(chapter)} · Chunk {value.chunk}/{value.chunks}</p>)}
      <ErrorNotice detail={job.error_detail ?? job.error} />
      {active ? <button className="btn btn-ghost" disabled={busy} onClick={() => void action('cancel')}>Cancel</button>
        : job.status !== 'done' && <button className="btn btn-primary" disabled={busy} onClick={() => void action('resume')}>Resume batch</button>}
      {job.status === 'done' && <div className="translation-downloads">
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/translated.json`}>Translated Chapters</a>
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/dictionary.json`}>Updated Book Dictionary</a>
      </div>}
      <TranslationLog logs={job.logs} />
    </div>}
    <ErrorNotice detail={error} />
  </section>;
}
