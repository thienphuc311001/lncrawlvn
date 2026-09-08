export default function CtaStrip() {
  return (
    <section className="section" data-od-id="cta-strip" style={{ textAlign: 'center' }}>
      <div className="container" style={{ maxWidth: '600px' }}>
        <h2>Start extracting novels in one command.</h2>
        <p className="lead" style={{ margin: '16px auto 32px' }}>
          Open source, no signup required. Works offline after first install.
        </p>
        <button type="button" className="btn btn-primary">
          Download CLI
        </button>
      </div>
    </section>
  );
}
