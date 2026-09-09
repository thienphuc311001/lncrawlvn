'use client';

import { useState } from 'react';

import { useJobRunner } from './job-runner';

const EXAMPLE_URL = 'https://xtruyen.vn/truyen/huyen-giam-tien-toc/';

export default function Hero() {
  const [url, setUrl] = useState('');
  const [startError, setStartError] = useState('');
  const { job, jobStatus, startJob } = useJobRunner();
  const isRunning = job !== null && jobStatus === 'running';

  const start = () => {
    if (isRunning) return;
    const target = url.trim();
    if (!target) {
      setStartError('Nhập URL truyện trước khi extract.');
      return;
    }
    setStartError('');
    startJob(target).catch((error: unknown) => {
      setStartError(error instanceof Error ? error.message : String(error));
    });
  };

  const seeExample = () => {
    if (isRunning) return;
    setUrl(EXAMPLE_URL);
    setStartError('');
  };

  return (
    <section className="section" id="hero" data-od-id="hero-split">
      <div className="container">
        <p className="eyebrow">WEB NOVEL EXTRACTION</p>
        <h1>Paste a URL. Get an EPUB.</h1>
        <p className="lead" style={{ marginTop: '20px', maxWidth: '52ch' }}>
          Extract chapters from web novel sites, convert to EPUB/PDF/MOBI, and read offline on any
          device.
        </p>
        <div style={{ marginTop: '32px' }}>
          <div className="field">
            <label htmlFor="novel-url">Novel URL</label>
            <input
              type="url"
              id="novel-url"
              className="input"
              placeholder={EXAMPLE_URL}
              style={{ fontFamily: 'var(--font-mono)', fontSize: '14px' }}
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') start();
              }}
            />
          </div>
          <div className="hero-cta" style={{ marginTop: '20px' }}>
            <button type="button" className="btn btn-primary" onClick={start} disabled={isRunning}>
              {isRunning ? 'Crawling…' : 'Extract novel'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-arrow"
              onClick={seeExample}
              disabled={isRunning}
            >
              See example
            </button>
          </div>
          {startError && (
            <p className="muted error-text" style={{ marginTop: '12px' }}>
              ✗ {startError}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
