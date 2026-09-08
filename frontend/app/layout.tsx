import type { Metadata } from 'next';

import './globals.css';

export const metadata: Metadata = {
  title: 'Novel Crawler · Extract, convert, and download web novels',
  description:
    'Extract chapters from web novel sites, convert to EPUB/PDF/MOBI, and read offline on any device.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
