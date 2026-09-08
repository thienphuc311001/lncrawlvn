export default function Features() {
  return (
    <section className="section" id="features" data-od-id="features">
      <div className="container stack" style={{ gap: '56px' }}>
        <div style={{ maxWidth: '36ch' }}>
          <p className="eyebrow">BUILT FOR READERS</p>
          <h2>Extract once. Read anywhere.</h2>
        </div>
        <div className="grid-3">
          <div className="feature card-flat">
            <div className="feature-mark">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
            </div>
            <h3>Smart parsing</h3>
            <p>
              Automatically detects chapter structure, pagination patterns, and navigation across
              200+ novel platforms without manual configuration.
            </p>
          </div>
          <div className="feature card-flat">
            <div className="feature-mark">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}>
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </div>
            <h3>Multiple formats</h3>
            <p>
              Export to EPUB, PDF, MOBI, or plain Markdown. Preserves formatting, chapter breaks,
              and author metadata for clean offline reading.
            </p>
          </div>
          <div className="feature card-flat">
            <div className="feature-mark">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}>
                <rect x="3" y="3" width="7" height="7" />
                <rect x="14" y="3" width="7" height="7" />
                <rect x="14" y="14" width="7" height="7" />
                <rect x="3" y="14" width="7" height="7" />
              </svg>
            </div>
            <h3>Batch processing</h3>
            <p>
              Queue multiple novels, resume interrupted downloads, and deduplicate chapters
              automatically. Runs in the background without blocking.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
