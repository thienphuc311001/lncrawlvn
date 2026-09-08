export default function TopNav() {
  return (
    <header className="topnav" data-od-id="topnav">
      <div className="container topnav-inner">
        <span className="logo">NovelCrawler</span>
        <nav>
          <a href="#features">Features</a>
          <a href="#">Docs</a>
          <a href="#">GitHub</a>
        </nav>
        <a className="btn btn-primary" href="#hero">
          Start crawling
        </a>
      </div>
    </header>
  );
}
