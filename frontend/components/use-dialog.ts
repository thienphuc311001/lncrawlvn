'use client';

import { useEffect, useRef } from 'react';

/** Keep keyboard focus inside an open dialog and return it to its trigger. */
export default function useDialog(open: boolean, onClose: () => void) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const triggerRef = useRef<HTMLElement | null>(null);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    if (!open) {
      // A trigger can temporarily lose focus while its async request disables it.
      const rememberTrigger = (event: FocusEvent) => {
        if (event.target instanceof HTMLElement && event.target !== document.body) {
          triggerRef.current = event.target;
        }
      };
      document.addEventListener('focusin', rememberTrigger);
      return () => document.removeEventListener('focusin', rememberTrigger);
    }
    const active = document.activeElement as HTMLElement | null;
    const previous = active && active !== document.body ? active : triggerRef.current;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
    )).filter(element => element.getClientRects().length > 0);
    (dialog.querySelector<HTMLElement>('[data-dialog-initial-focus]') ?? focusable()[0] ?? dialog).focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      }
      if (event.key !== 'Tab') return;
      const elements = focusable();
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); dialog.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    dialog.addEventListener('keydown', handleKey);
    return () => {
      dialog.removeEventListener('keydown', handleKey);
      document.body.style.overflow = previousOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  return dialogRef;
}
