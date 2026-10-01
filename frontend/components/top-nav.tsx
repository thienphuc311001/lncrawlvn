'use client';

import { useLayoutEffect, useRef } from 'react';
import Icon from './icon';

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
        <a className="skip-link" href="#content">Skip to content</a>
        <a className="logo" href="/" aria-label="NovelCrawler home">
          <span className="logo-mark"><Icon name="book" size={23} /></span>
          NovelCrawler <span className="logo-label">Your reading workspace</span>
        </a>
        <nav aria-label="Project links">
          <a href="https://github.com/thienphuc311001/lncrawlvn#readme" target="_blank" rel="noreferrer">Guide</a>
          <a href="https://github.com/thienphuc311001/lncrawlvn" target="_blank" rel="noreferrer">GitHub <Icon name="external" size={14} /></a>
        </nav>
      </div>
    </header>
  );
}
