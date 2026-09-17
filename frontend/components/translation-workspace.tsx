'use client';

import { useEffect, useState } from 'react';

const API = 'http://127.0.0.1:8000/api/translation';
type Job = {
  job_id: string; status: string; stage?: string; error?: string;
  total_chapters?: number; completed_chapters?: number; aligned?: number;
  scanned_chapter?: number;
  active_chapters?: Record<string, { chunk: number; chunks: number }>;
};
type Config = { models: Record<string, string>; concurrency: number; stagger_ms: number; api_key_configured: boolean };

async function request(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`${API}${path}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

export default function TranslationWorkspace() {
  const [files, setFiles] = useState<Record<string, File | undefined>>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const active = job?.status === 'pending' || job?.status === 'running';

  useEffect(() => {
    let live = true;
    Promise.all([request('/config'), request('/jobs')]).then(([configuration, history]) => {
      if (!live) return;
      setConfig(configuration as Config);
      setJobs(history as Job[]);
      setJob((history as Job[])[0] ?? null);
    }).catch(e => { if (live) setError(String(e.message)); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!job || !active) return;
    let live = true;
    const timer = setInterval(() => {
      request(`/jobs/${job.job_id}`).then(data => {
        if (live) setJob(data as Job);
      }).catch(e => { if (live) setError(String(e.message)); });
    }, 1500);
    return () => { live = false; clearInterval(timer); };
  }, [job?.job_id, active]);

  async function start() {
    setBusy(true); setError('');
    try {
      if (!files.raw || !files.vietphrase) throw new Error('Select RAW and VIETPHRASE text files.');
      const [raw, vietphrase, dictionary] = await Promise.all([
        files.raw.text(), files.vietphrase.text(), files.dictionary?.text(),
      ]);
      const result = await request('/jobs', { raw, vietphrase, dictionary: dictionary ? JSON.parse(dictionary) : null }) as Job;
      setJob(result);
      setJobs(previous => [result, ...previous.filter(j => j.job_id !== result.job_id)]);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function action(name: 'cancel' | 'resume') {
    if (!job) return;
    setBusy(true); setError('');
    try { setJob(await request(`/jobs/${job.job_id}/${name}`, {}) as Job); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
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
    <p className="muted">UTF-8 text with matching numbered chapter headings (第1章 / Chương 1 / Chapter 1). The dictionary is cumulative JSON.</p>
    <button className="btn btn-primary" disabled={busy || active || !config?.api_key_configured || !files.raw || !files.vietphrase} onClick={() => void start()}>
      {busy ? 'Working…' : 'Translate batch'}
    </button>
    {config && <details className="translation-config"><summary>System Configuration</summary>
      {Object.entries(config.models).map(([role, model]) => <label className="settings-field" key={role}>
        <span>{role[0].toUpperCase() + role.slice(1)} Model</span><input readOnly value={model} />
      </label>)}
    </details>}
    {config && <p className="muted">{config.concurrency} workers · {config.stagger_ms} ms staggering · Server API key {config.api_key_configured ? 'configured' : 'missing — set GOOGLE_AI_API_KEY on the server'}</p>}
    {jobs.length > 0 && <label className="settings-field"><span>Saved batches</span><select value={job?.job_id ?? ''} disabled={busy}
      onChange={e => { request(`/jobs/${e.target.value}`).then(data => setJob(data as Job)).catch(e => setError(String(e.message))); }}>
      {jobs.map(j => <option key={j.job_id} value={j.job_id}>{j.job_id.slice(0, 12)}</option>)}
    </select></label>}
    {job && <div className="translation-progress" aria-live="polite">
      <h2>{job.stage ?? job.status}</h2>
      <p>{job.status} · {job.completed_chapters ?? 0}/{job.total_chapters ?? '?'} chapters finalized</p>
      {job.aligned !== undefined && <p>Aligned chapters: {job.aligned}</p>}
      {Object.entries(job.active_chapters ?? {}).map(([chapter, value]) => <p key={chapter}>Chapter {chapter} · Chunk {value.chunk}/{value.chunks}</p>)}
      {job.error && <p role="alert" className="error-text">{job.error}</p>}
      {active ? <button className="btn btn-ghost" disabled={busy} onClick={() => void action('cancel')}>Cancel</button>
        : job.status !== 'done' && <button className="btn btn-primary" disabled={busy} onClick={() => void action('resume')}>Resume batch</button>}
      {job.status === 'done' && <div className="translation-downloads">
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/translated.json`}>Translated Chapters</a>
        <a className="btn btn-primary" href={`${API}/jobs/${job.job_id}/outputs/dictionary.json`}>Updated Book Dictionary</a>
      </div>}
    </div>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </section>;
}
