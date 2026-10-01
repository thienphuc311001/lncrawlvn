'use client';

import { useLayoutEffect, useRef, useState } from 'react';

import Hero from './hero';
import BookLibrary from './book-library';
import TranslationWorkspace from './translation-workspace';
import Icon from './icon';

type Tab = 'extract' | 'books' | 'translate';

export default function AppTabs() {
  const [tab, setTab] = useState<Tab>('extract');
  const [bookId, setBookId] = useState<string | null>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const navigate = (next: Tab) => {
    setTab(next);
    window.scrollTo({ top: 0, behavior: 'instant' });
  };

  useLayoutEffect(() => {
    const tabs = tabsRef.current;
    if (!tabs) return;

    const updateHeight = () => {
      document.documentElement.style.setProperty('--app-tabs-height', `${tabs.getBoundingClientRect().height}px`);
    };
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(tabs);
    return () => {
      observer.disconnect();
      document.documentElement.style.removeProperty('--app-tabs-height');
    };
  }, []);

  return (
    <>
      <div ref={tabsRef} className="app-tabs">
        <div className="container app-tabs-inner">
          <nav className="workspace-nav" aria-label="Workspace">
            <button
              type="button"
              className={`tab-btn${tab === 'extract' ? ' active' : ''}`}
              onClick={() => navigate('extract')}
              aria-current={tab === 'extract' ? 'page' : undefined}
            >
              <Icon name="link" size={18} />
              Extract
            </button>
            <button
              type="button"
              className={`tab-btn${tab === 'books' ? ' active' : ''}`}
              onClick={() => { setBookId(null); navigate('books'); }}
              aria-current={tab === 'books' ? 'page' : undefined}
            >
              <Icon name="book" size={18} />
              Books
            </button>
            <button type="button" className={`tab-btn${tab === 'translate' ? ' active' : ''}`} onClick={() => navigate('translate')} aria-current={tab === 'translate' ? 'page' : undefined}>
              <Icon name="translate" size={18} />
              Translate
            </button>
          </nav>
          <span className="workspace-note">A personal library. One chapter at a time.</span>
        </div>
      </div>
      {tab === 'extract' ? (
        <Hero onGoBooks={(id) => { setBookId(id ?? null); navigate('books'); }} />
      ) : tab === 'translate' ? (
        <TranslationWorkspace />
      ) : (
        <BookLibrary initialBookId={bookId} onGoExtract={() => navigate('extract')} />
      )}
    </>
  );
}
