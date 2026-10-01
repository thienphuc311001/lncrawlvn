import type { Metadata } from 'next';

import './globals.css';

export const metadata: Metadata = {
  title: 'NovelCrawler · Your reading workspace',
  description:
    'Build your personal web novel library, translate Chinese chapters into Vietnamese, and export EPUB or TXT for offline reading.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // suppressHydrationWarning: browser extensions (e.g. Trancy) inject
  // attributes like trancy-version onto <html> before React hydrates.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
