import type { CSSProperties } from 'react';

type IconName = 'book' | 'link' | 'download' | 'arrow' | 'settings' | 'translate' | 'search' | 'refresh' | 'check' | 'external';

export default function Icon({ name, size = 20, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  const paths: Record<IconName, React.ReactNode> = {
    book: <><path d="M12 5.5C9 3.5 5 3.5 2.5 4.5v14C5 17.5 9 17.5 12 19.5c3-2 7-2 9.5-1v-14C19 3.5 15 3.5 12 5.5Z" /><path d="M12 5.5v14M6 8h3M6 11h3M15 8h3M15 11h3" /></>,
    link: <><path d="m10 13 4-4M8 15l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 9l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(0 -1)" /></>,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4" /></>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    settings: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="currentColor" stroke="none" /><circle cx="15" cy="17" r="3" fill="currentColor" stroke="none" /></>,
    translate: <><path d="M3 5h11M8 3v2m-3 3c1 4 4 7 8 9M12 5c0 5-4 10-9 12m11 4 4-11 4 11m-6.5-4h5" /></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    external: <><path d="M14 3h7v7m0-7L10 14M10 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>{paths[name]}</svg>;
}
