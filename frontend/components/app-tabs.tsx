'use client';

import { useState } from 'react';

import Hero from './hero';
import BookLibrary from './book-library';

type Tab = 'extract' | 'books';

export default function AppTabs() {
  const [tab, setTab] = useState<Tab>('extract');

  return (
    <>
      <div className="app-tabs">
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
        </div>
      </div>
      {tab === 'extract' ? (
        <Hero />
      ) : (
        <BookLibrary onGoExtract={() => setTab('extract')} />
      )}
    </>
  );
}
