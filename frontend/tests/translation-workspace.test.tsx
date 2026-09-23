// Run from frontend: bun test tests/translation-workspace.test.tsx
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const { Bun } = globalThis as unknown as {
  Bun: { Transpiler: new (options: object) => { transformSync(source: string): string } };
};
const source = readFileSync(new URL('../components/translation-workspace.tsx', import.meta.url), 'utf8');
const reactImport = /import \{[^}]+\} from 'react';/;
const script = new Bun.Transpiler({
  loader: 'tsx', target: 'node', tsconfig: { compilerOptions: { jsx: 'react' } },
}).transformSync(source.replace(reactImport, '').replace('export default function', 'function')
  + '\nmodule.exports = { TranslationWorkspace, ErrorNotice };');

function render(job: Record<string, unknown> | null = null) {
  const module = { exports: {} as {
    TranslationWorkspace: typeof import('../components/translation-workspace').default;
    ErrorNotice: React.ComponentType<{ detail: unknown }>;
  } };
  const states = [{}, [], job, { models: { primary: 'gemini-3.1-flash-lite', fallback: 'gemini-3.5-flash-lite' },
    concurrency: 2, stagger_ms: 300, api_key_configured: true }, null, false, true, false];
  let stateIndex = 0;
  runInNewContext(script, {
    module, React, useState: () => [states[stateIndex++], () => {}],
    useRef: React.useRef, useId: React.useId, useEffect: () => {},
  });
  return {
    html: renderToStaticMarkup(React.createElement(module.exports.TranslationWorkspace)),
    ErrorNotice: module.exports.ErrorNotice,
  };
}

test('translation workspace requests RAW and optional dictionary only', () => {
  const { html } = render();
  assert.ok(html.includes('RAW'));
  assert.ok(html.includes('DICTIONARY'));
  assert.ok(!html.includes('VIETPHRASE'));
  assert.equal((html.match(/type="file"/g) ?? []).length, 2);
});

test('completed batch offers merged dictionary and unresolved artifacts', () => {
  const { html } = render({
    job_id: 'abc', status: 'done', total_chapters: 2, completed_chapters: 2,
    outputs: { translation: '/translated.txt', dictionary: '/dictionary.json', unresolved: '/unresolved.json' },
    logs: [],
  });
  assert.ok(html.includes('Full merged dictionary JSON'));
  assert.ok(html.includes('/dictionary.json'));
  assert.ok(html.includes('Unresolved terms JSON'));
});

test('completed batch shows its filenames on download links', () => {
  const { html } = render({
    job_id: 'abc', status: 'done', logs: [],
    output_filenames: {
      translation: '0141-0150-translated.txt',
      dictionary: '0141-0150-dictionary.json',
      unresolved: '0141-0150-unresolved.json',
    },
  });
  assert.ok(html.includes('0141-0150-translated.txt'));
  assert.ok(html.includes('0141-0150-dictionary.json'));
  assert.ok(html.includes('0141-0150-unresolved.json'));
  assert.ok(html.includes('download="0141-0150-translated.txt"'));
});

test('request breakdown, anomaly, and separated notes are visible', () => {
  const { html } = render({
    job_id: 'abc', status: 'done', total_chapters: 2, completed_chapters: 2,
    outputs: { translation: '/translated.txt', dictionary: '/dictionary.json',
      unresolved: '/unresolved.json', author_notes: '/author-notes.txt' },
    request_warning: { code: 'REQUEST_BUDGET_ANOMALY', severity: 'warning', logical_calls: 20 },
    request_statistics: {
      total_requests: 24, requests: {}, logical_operations: {}, local_ai_requests: {},
      retry: 2, model_fallback: 1, cache_hits: 3,
      api_attempt_count: 24, technical_retry_count: 2, fallback_count: 1, cache_hit_count: 3,
      by_operation: { translation: { logical_calls: 18, api_attempts: 20,
        technical_retries: 2, fallbacks: 1, cache_hits: 3 } },
    },
    logs: [],
  });
  assert.ok(html.includes('Author notes TXT'));
  assert.ok(html.includes('Request budget anomaly'));
  assert.ok(html.includes('24 API attempts'));
  assert.ok(html.includes('translation'));
  assert.ok(html.includes('Technical retries: 2'));
});

test('structured parser error is rendered safely', () => {
  const { ErrorNotice } = render();
  const html = renderToStaticMarkup(React.createElement(ErrorNotice, {
    detail: { error_type: 'duplicate_chapter', chapter: 62, line: 5363,
      previous_line: 5360, message: '<script>bad</script>' },
  }));
  assert.ok(html.includes('duplicate_chapter'));
  assert.ok(html.includes('5363'));
  assert.ok(html.includes('&lt;script&gt;bad&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
});

test('failed paragraph offers explicit repair or audited acceptance without deleting it', () => {
  const { html } = render({
    job_id: 'abc', status: 'failed', logs: [],
    manual_review: {
      chapter: 145, paragraph_id: 'P0145_0062', raw: '会试结束。',
      current_text: 'Thi hội đã kết thúc.', fingerprint: 'abc',
      findings: [{ kind: 'locked_term_missing', source: '会试', required: 'hội thí' }],
    },
  });
  assert.ok(html.includes('P0145_0062'));
  assert.ok(html.includes('会试结束。'));
  assert.ok(html.includes('Thi hội đã kết thúc.'));
  assert.ok(html.includes('hội thí'));
  assert.ok(html.includes('Lưu bản sửa và tiếp tục'));
  assert.ok(html.includes('Chấp nhận bản hiện tại và tiếp tục'));
  assert.ok(!html.includes('Resume batch'));
});
