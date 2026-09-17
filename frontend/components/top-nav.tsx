'use client';

import { useLayoutEffect, useRef } from 'react';

export default function TopNav() {
  const headerRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const header = headerRef.current;
    if (!header) return;

    const updateHeight = () => {
      document.documentElement.style.setProperty('--topnav-height', `${header.getBoundingClientRect().height}px`);
    };
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(header);
    return () => {
      observer.disconnect();
      document.documentElement.style.removeProperty('--topnav-height');
    };
  }, []);

  return (
    <header ref={headerRef} className="topnav" data-od-id="topnav">
      <div className="container topnav-inner">
        <span className="logo">NovelCrawler</span>
        <nav>
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
