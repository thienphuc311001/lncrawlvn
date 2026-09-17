'use client';

import { useLayoutEffect, useRef, useState } from 'react';

import Hero from './hero';
import BookLibrary from './book-library';
import TranslationWorkspace from './translation-workspace';

type Tab = 'extract' | 'books' | 'translate';

export default function AppTabs() {
  const [tab, setTab] = useState<Tab>('extract');
  const tabsRef = useRef<HTMLDivElement>(null);

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
          <button
            type="button"
            className={`tab-btn${tab === 'extract' ? ' active' : ''}`}
            onClick={() => setTab('extract')}
          >
            Extract
          </button>
          <button
            type="button"
            className={`tab-btn${tab === 'books' ? ' active' : ''}`}
            onClick={() => setTab('books')}
          >
            Books
          </button>
          <button type="button" className={`tab-btn${tab === 'translate' ? ' active' : ''}`} onClick={() => setTab('translate')}>
            Translate
          </button>
        </div>
      </div>
      {tab === 'extract' ? (
        <Hero />
      ) : tab === 'translate' ? (
        <TranslationWorkspace />
      ) : (
        <BookLibrary onGoExtract={() => setTab('extract')} />
      )}
    </>
  );
}
