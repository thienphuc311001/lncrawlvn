'use client';

import { useEffect, useState } from 'react';

import type { ExtractOptions } from './job-runner';

const API_BASE = 'http://127.0.0.1:8000';

type ConfigField = {
  key: string;
  type: 'int' | 'float' | 'bool' | 'text';
  label: string;
  help: string;
  value: unknown;
  default: unknown;
  min?: number;
  max?: number;
  step?: number;
};

type SettingsModalProps = {
  open: boolean;
  onClose: () => void;
  onCrawlOptionsChange: (opts: ExtractOptions) => void;
};

export default function SettingsModal({ open, onClose, onCrawlOptionsChange }: SettingsModalProps) {
  const [fields, setFields] = useState<ConfigField[]>([]);
  const [crawl, setCrawl] = useState<ExtractOptions>({});
  const [engine, setEngine] = useState<Record<string, unknown>>({});
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    if (!open || initialized) return;
    void (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/config`);
        if (!res.ok) throw new Error(`API returned ${res.status}`);
        const list: ConfigField[] = await res.json();
        setFields(list);
        setEngine(Object.fromEntries(list.map((f) => [f.key, f.value])));
        setInitialized(true);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [open, initialized]);

  const close = () => {
    onClose();
    setError('');
    setSaved(false);
  };

  const updateCrawl = (key: keyof ExtractOptions, value: number | boolean | string) => {
    const next: ExtractOptions = { ...crawl };
    const numberKeys: (keyof ExtractOptions)[] = ['first', 'last', 'rate_limit', 'workers'];
    const boolKeys: (keyof ExtractOptions)[] = ['save', 'overwrite'];
    if (numberKeys.includes(key) && typeof value === 'string') {
      const parsed = value === '' ? undefined : Number(value);
      (next as Record<string, unknown>)[key] = parsed;
    } else if (boolKeys.includes(key)) {
      (next as Record<string, unknown>)[key] = Boolean(value);
    }
    setCrawl(next);
    onCrawlOptionsChange(next);
  };

  const pushEngine = async (key: string, raw: unknown) => {
    try {
      setError('');
      const res = await fetch(`${API_BASE}/api/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: raw }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        const msg = detail?.detail?.[key];
        throw new Error(typeof msg === 'string' ? msg : `API returned ${res.status}`);
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 1200);
      setEngine((prev) => ({ ...prev, [key]: raw }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (!open) return null;

  return (
    <div className="settings-overlay" role="dialog" aria-modal="true" aria-label="Settings">
      <div className="settings-modal">
        <div className="settings-head">
          <h3>⚙ Settings</h3>
          <button type="button" className="btn btn-ghost" onClick={close} aria-label="Đóng">
            ✕
          </button>
        </div>

        <section className="settings-group">
          <h4>Extract tweaks</h4>
          <div className="settings-grid">
            <label className="settings-field">
              <span>First chapters</span>
              <input
                type="number"
                min={1}
                value={crawl.first ?? ''}
                onChange={(e) => updateCrawl('first', e.target.value)}
                placeholder="all"
              />
            </label>
            <label className="settings-field">
              <span>Last chapters</span>
              <input
                type="number"
                min={1}
                value={crawl.last ?? ''}
                onChange={(e) => updateCrawl('last', e.target.value)}
                placeholder="all"
              />
            </label>
            <label className="settings-field">
              <span>Rate limit (req/s)</span>
              <input
                type="number"
                min={0.05}
                step={0.05}
                value={crawl.rate_limit ?? ''}
                onChange={(e) => updateCrawl('rate_limit', e.target.value)}
                placeholder="source default"
              />
            </label>
            <label className="settings-field">
              <span>Workers (song song)</span>
              <input
                type="number"
                min={1}
                max={16}
                value={crawl.workers ?? ''}
                onChange={(e) => updateCrawl('workers', e.target.value)}
                placeholder="engine default"
              />
            </label>
            <label className="settings-check">
              <input
                type="checkbox"
                checked={crawl.save !== false}
                onChange={(e) => updateCrawl('save', e.target.checked)}
              />
              <span>Save to library</span>
            </label>
            <label className="settings-check">
              <input
                type="checkbox"
                checked={crawl.overwrite === true}
                onChange={(e) => updateCrawl('overwrite', e.target.checked)}
              />
              <span>Ghi đè chương đã lưu (repair re-crawl)</span>
            </label>
          </div>
        </section>

        <section className="settings-group">
          <h4>Engine</h4>
          <p className="muted settings-hint">Thay đổi được áp ngay và lưu vào settings.json.</p>
          <div className="settings-grid">
            {fields.map((field) => (
              <label key={field.key} className="settings-field settings-field-nolabel">
                <span className="mono">{field.key}</span>
                {field.type === 'bool' ? (
                  <input
                    type="checkbox"
                    checked={Boolean(engine[field.key])}
                    onChange={(e) => void pushEngine(field.key, e.target.checked)}
                  />
                ) : field.type === 'text' ? (
                  <input
                    type="text"
                    value={String(engine[field.key] ?? '')}
                    onChange={(e) => void pushEngine(field.key, e.target.value)}
                  />
                ) : (
                  <input
                    type="number"
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    value={String(engine[field.key] ?? '')}
                    onChange={(e) => {
                      const raw = e.target.value;
                      const parsed = field.type === 'int' ? Number(raw) : Number(raw);
                      void pushEngine(field.key, parsed);
                    }}
                  />
                )}
                {field.help && <small>{field.help}</small>}
              </label>
            ))}
          </div>
        </section>

        {error && (
          <p className="muted error-text" role="alert">
            ✗ {error}
          </p>
        )}
        {saved && <p className="muted settings-saved">✓ Saved</p>}
        <div className="settings-actions">
          <button type="button" className="btn btn-primary" onClick={close}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}