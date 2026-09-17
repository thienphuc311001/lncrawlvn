// Run from frontend: bun test tests/translation-workspace.test.tsx
// Offline: existing React + Bun only; no server, model calls, or emitted files.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import React, { type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// A local runtime shape keeps Next's type-check independent of @types/bun.
const { Bun } = globalThis as unknown as {
  Bun: { Transpiler: new (options: object) => { transformSync(source: string): string } };
};
const source = readFileSync(new URL('../components/translation-workspace.tsx', import.meta.url), 'utf8');
const reactImport = /import \{[^}]+\} from 'react';/;
assert.match(source, reactImport, 'Update the test hook adapter if the React import changes.');
const script = new Bun.Transpiler({
  loader: 'tsx', target: 'node', tsconfig: { compilerOptions: { jsx: 'react' } },
}).transformSync(source.replace(reactImport, '').replace('export default function', 'function')
  + '\nmodule.exports = { TranslationWorkspace, ErrorNotice, request, errorDetail };');

type Workspace = typeof import('../components/translation-workspace').default;
type TestModule = {
  TranslationWorkspace: Workspace;
  ErrorNotice: (props: { detail: unknown }) => ReactNode;
  request: (path: string, body?: unknown) => Promise<unknown>;
  errorDetail: (error: unknown) => unknown;
};

function load({ job = null, error = null, detail = null, status = 422 }: {
  job?: Record<string, unknown> | null; error?: unknown; detail?: unknown; status?: number;
} = {}) {
  const module = { exports: {} as TestModule };
  // Mirror the six workspace states: files, jobs, selected job, config, error, busy.
  const states = [{}, [], job, null, error, false];
  let stateIndex = 0;
  const calls: string[] = [];
  runInNewContext(script, {
    module, React,
    useState: () => {
      assert.ok(stateIndex < states.length, 'Update state fixtures if workspace hooks change.');
      return [states[stateIndex++], () => {}];
    },
    useEffect: () => {}, // No polling/network effects during an offline render.
    fetch: async (url: string) => {
      calls.push(url);
      return { ok: status < 400, status, json: async () => ({ detail }) };
    },
  });
  return { ...module.exports, calls };
}

const structured = {
  error: 'chapter_order', severity: 'error', input: 'RAW', line: 14,
  heading: '<script>not executable</script>', volume: 2, chapter: 3,
  previous_chapter: 5, previous_line: 8,
  counterpart: { input: 'VIETPHRASE', line: 12 },
  reason: 'Chapter number decreased', message: 'Check the source heading',
};

function assertStructured(html: string) {
  for (const label of ['Error', 'Severity', 'Input', 'Line', 'Heading', 'Volume', 'Chapter',
    'Previous chapter', 'Previous line', 'Counterpart', 'Reason', 'Message']) {
    assert.ok(html.includes(`<dt><strong>${label}</strong></dt>`), label);
  }
  for (const text of ['<dd>RAW</dd>', '<dd>14</dd>', '<dd>2</dd>', 'VIETPHRASE',
    'Chapter number decreased', 'Check the source heading']) assert.ok(html.includes(text), text);
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes('&lt;script&gt;not executable&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('{&quot;error&quot;'), 'Do not stringify the detail object.');
}

test('HTTP structured details survive request errors and render readable fields', async () => {
  const api = load({ detail: structured });
  await assert.rejects(api.request('/jobs', {}), error => {
    const detail = api.errorDetail(error);
    assert.equal(detail, structured);
    assertStructured(renderToStaticMarkup(React.createElement(api.ErrorNotice, { detail })));
    return true;
  });
  assert.deepEqual(api.calls, ['http://127.0.0.1:8000/api/translation/jobs']);
});

test('Pydantic detail arrays and legacy string HTTP errors remain readable', async () => {
  for (const detail of [
    [{ loc: ['body', 'raw'], msg: 'Field required', type: 'missing' }],
    'Server key missing',
  ]) {
    const api = load({ detail });
    await assert.rejects(api.request('/jobs', {}), error => {
      const html = renderToStaticMarkup(React.createElement(api.ErrorNotice, { detail: api.errorDetail(error) }));
      if (typeof detail === 'string') assert.ok(html.includes(detail));
      else {
        assert.ok(html.includes('<ul><li>'));
        assert.ok(html.includes('body → raw:'));
        assert.ok(html.includes('Field required'));
      }
      return true;
    });
  }
});

test('background job detail takes precedence over the legacy error string', () => {
  const api = load({ job: { job_id: 'test', status: 'failed', error: 'LEGACY FALLBACK', error_detail: structured } });
  const html = renderToStaticMarkup(React.createElement(api.TranslationWorkspace));
  assertStructured(html);
  assert.ok(!html.includes('LEGACY FALLBACK'));
  assert.deepEqual(api.calls, []);
});

test('scoped and legacy active chapters show readable chunk progress', () => {
  const api = load({ job: {
    job_id: 'test', status: 'running', completed_chapters: 1, total_chapters: 3,
    active_chapters: { 'v2-c3': { chunk: 1, chunks: 4 }, '11': { chunk: 2, chunks: 5 } },
  } });
  const html = renderToStaticMarkup(React.createElement(api.TranslationWorkspace));
  assert.ok(html.includes('Volume 2 / Chapter 3 · Chunk 1/4'));
  assert.ok(html.includes('Chapter 11 · Chunk 2/5'));
  assert.ok(html.includes('1/3 chapters finalized'));
  assert.ok(!html.includes('Chapter v2-c3'));
  assert.deepEqual(api.calls, []);
});

test('legacy background errors and absent/null context have safe fallbacks', () => {
  const api = load({ job: { job_id: 'test', status: 'failed', error: 'Legacy job failure', error_detail: null } });
  assert.ok(renderToStaticMarkup(React.createElement(api.TranslationWorkspace)).includes('Legacy job failure'));
  for (const detail of [null, undefined, '']) {
    assert.equal(renderToStaticMarkup(React.createElement(api.ErrorNotice, { detail })), '');
  }
  const html = renderToStaticMarkup(React.createElement(api.ErrorNotice, { detail: { line: null, chapter: 0 } }));
  assert.ok(html.includes('Not available'));
  assert.ok(html.includes('<dd>0</dd>'));
});

test('model attempts, fallback reasons and final failure are visible in activity logs', () => {
  const api = load({ job: { job_id: 'test', status: 'failed', logs: [
    { id: '1', timestamp: '2026-09-17T01:00:00+00:00', status: 'running', task: 'translate:1', model: 'gemini-3.1-flash-lite', attempt: 1 },
    { id: '2', status: 'failed', model: 'gemini-3.1-flash-lite', error: 'Daily quota exhausted' },
    { id: '3', status: 'fallback', model: 'gemini-3.5-flash-lite', message: 'Switching to fallback' },
    { id: '4', status: 'failed', model: 'gemini-3.7-flash', error: 'HTTP 503 <unsafe>' },
  ] } });
  const html = renderToStaticMarkup(React.createElement(api.TranslationWorkspace));
  for (const text of ['Translation activity', 'role="log"', 'gemini-3.1-flash-lite', 'gemini-3.5-flash-lite', 'gemini-3.7-flash', 'Attempt 1/2', 'Daily quota exhausted', 'Switching to fallback', 'HTTP 503 &lt;unsafe&gt;']) assert.ok(html.includes(text), text);
  assert.ok(html.indexOf('HTTP 503') < html.indexOf('Switching to fallback'), 'Newest event appears first');
});
