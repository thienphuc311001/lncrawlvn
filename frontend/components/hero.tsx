'use client';

import { useState } from 'react';

import DemoOutput from './demo-output';

const EXAMPLE_URL = 'https://xtruyen.vn/truyen/huyen-giam-tien-toc/';

export default function Hero() {
  const [url, setUrl] = useState('');
  const [runToken, setRunToken] = useState(0);
  const [isRunning, setIsRunning] = useState(false);

  const start = () => {
    if (isRunning) return;
    setRunToken((token) => token + 1);
  };

  const seeExample = () => {
    if (isRunning) return;
    setUrl(EXAMPLE_URL);
    setRunToken((token) => token + 1);
  };

  return (
    <section className="section" id="hero" data-od-id="hero-split">
      <div className="container hero-split">
        <div>
          <p className="eyebrow">WEB NOVEL EXTRACTION</p>
          <h1>Paste a URL. Get an EPUB.</h1>
          <p className="lead" style={{ marginTop: '20px' }}>
            Extract chapters from web novel sites, convert to EPUB/PDF/MOBI, and read offline on
            any device.
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
          </div>
        </div>
        <DemoOutput url={url.trim()} runToken={runToken} onRunningChange={setIsRunning} />
      </div>
    </section>
  );
}
