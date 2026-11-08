/**
 * Unit tests for the glossary suggestions review (portal/static/js/glossary-suggestions.js, #1172).
 *
 * Run with: node tests/test_glossary_suggestions.mjs
 */
import assert from 'node:assert/strict';

import {
  ACCEPTED,
  PENDING,
  REJECTED,
  acceptedPayload,
  applyErrors,
  checkSourceFile,
  initGlossarySuggestions,
  keepAsIs,
  reviewRows,
  translationNote,
} from '../portal/static/js/glossary-suggestions.js';

// A just-enough DOM: elements with children, attributes, classes and event handlers.
class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.dataset = {};
    this.classes = new Set();
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.classList = {
      toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)),
      contains: (name) => this.classes.has(name),
    };
  }

  addEventListener(type, handler) { this.handlers[type] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = String(value);
  }
  getAttribute(name) { return this.attributes[name]; }
}

globalThis.document = { createElement: (tag) => new FakeElement(tag) };

function walk(node, predicate, found = []) {
  if (predicate(node)) found.push(node);
  node.children.forEach((child) => walk(child, predicate, found));
  return found;
}

const LANGUAGES = [{ code: 'de', name: 'German' }, { code: 'fr', name: 'French' }];
const SUGGESTIONS = {
  languages: LANGUAGES,
  suggestions: [
    { source_term: 'NLLB', kind: 'acronym', occurrences: 4, match_type: 'exact', case_sensitive: true, in_glossary: [], translations: { de: 'NLLB', fr: 'NLLB' } },
    { source_term: 'Mission Control', kind: 'name', occurrences: 2, match_type: 'phrase', case_sensitive: false, in_glossary: ['fr'], translations: {} },
    { source_term: 'diarization', kind: 'rare', occurrences: 3, match_type: 'phrase', case_sensitive: false, in_glossary: [], translations: { de: 'Sprechertrennung' } },
  ],
  truncated: false,
  translation: { status: 'complete', service: 'OpenAI', failed_languages: [] },
};

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => (body instanceof Error ? Promise.reject(body) : body) };
}

function setup({ replies = [], translate = true } = {}) {
  const parts = Object.fromEntries(
    ['form', 'file', 'translate', 'submit', 'status', 'review', 'head', 'rows', 'save', 'accept-all', 'discard']
      .map((name) => [name, new FakeElement()]),
  );
  parts.translate.checked = translate;
  parts.file.files = [];
  const root = new FakeElement('section');
  root.hidden = true;
  root.dataset = { suggestUrl: '/workspace/events/1/rooms/2/ai-vocabulary/suggestions', saveUrl: '/workspace/events/1/rooms/2/ai-vocabulary/suggestions/save', maxBytes: '1048576' };
  root.querySelector = (selector) => parts[selector.match(/data-suggest-([\w-]+)/)[1]];
  const calls = [];
  const navigations = [];
  const pending = [...replies];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    const next = pending.shift();
    if (next instanceof Error) throw next;
    return typeof next === 'function' ? next() : next;
  };
  const state = initGlossarySuggestions(root, { fetch, navigate: (url) => navigations.push(url) });
  return { root, parts, state, calls, navigations };
}

async function upload(env, file = { name: 'Talk Script.pdf', size: 2048, type: 'application/pdf' }) {
  env.parts.file.files = [file];
  const prevented = { value: false };
  await env.parts.form.handlers.submit({ preventDefault() { prevented.value = true; } });
  assert.equal(prevented.value, true, 'the form never submits itself');
}

function rowElements(env) { return env.parts.rows.children; }
function input(tr, predicate) { return walk(tr, (node) => node.tagName === 'INPUT' && predicate(node))[0]; }
function button(tr, action) { return walk(tr, (node) => node.dataset.action === action)[0]; }
function type(element, value) { element.value = value; element.handlers.input(); }

// ── Pure helpers ─────────────────────────────────────────────────────────

assert.equal(checkSourceFile(null, 100), 'Choose a file first.');
assert.match(checkSourceFile({ name: 'slides.pptx', size: 10 }, 100), /Save slides as PDF/);
assert.match(checkSourceFile({ name: 'photo.png', size: 10 }, 100), /\.txt/);
assert.equal(checkSourceFile({ name: 'talk.md', size: 0 }, 100), 'The file is empty.');
assert.equal(checkSourceFile({ name: 'talk.PDF', size: 3 * 1024 * 1024 }, 2 * 1024 * 1024), 'Files must be 2 MB or smaller.');
for (const name of ['talk.txt', 'talk.md', 'talk.markdown', 'SLIDES.PDF']) assert.equal(checkSourceFile({ name, size: 10 }, 100), '');

{
  const rows = reviewRows(SUGGESTIONS);
  assert.deepEqual(rows.map((row) => [row.row, row.sourceTerm, row.decision]), [[1, 'NLLB', PENDING], [2, 'Mission Control', PENDING], [3, 'diarization', PENDING]]);
  assert.deepEqual(acceptedPayload(rows, LANGUAGES), { rows: [] }, 'nothing is saved until accepted');
  rows[1].decision = ACCEPTED;
  rows[2].decision = REJECTED;
  keepAsIs(rows[1], LANGUAGES);
  assert.deepEqual(rows[1].translations, { de: 'Mission Control' }, 'keep-as-is skips languages already in the glossary');
  assert.deepEqual(acceptedPayload(rows, LANGUAGES), {
    rows: [{ row: 2, source_term: 'Mission Control', match_type: 'phrase', case_sensitive: false, translations: { de: 'Mission Control' } }],
  });
  applyErrors(rows, [{ row: 2, language: 'de', message: 'Row 2: duplicate' }, { row: 99, message: 'ignored' }]);
  assert.deepEqual(rows.map((row) => row.errors), [[], ['Row 2: duplicate'], []]);
  assert.match(translationNote({ status: 'partial', service: 'OpenAI', failed_languages: ['fr'] }, LANGUAGES), /OpenAI couldn't suggest translations for French/);
  assert.equal(translationNote({ status: 'unavailable', service: null }, LANGUAGES), '');
}

// ── Upload, loading and display ──────────────────────────────────────────

{
  let release;
  const env = setup({ replies: [() => new Promise((resolve) => { release = () => resolve(response(200, SUGGESTIONS)); })] });
  assert.equal(env.root.hidden, false, 'the section appears once the script runs');
  assert.equal(env.parts.save.disabled, true);
  const done = upload(env);
  await Promise.resolve();
  assert.equal(env.parts.status.textContent, 'Reading the file and looking for terms…', 'a loading state shows while reading');
  assert.equal(env.parts.submit.disabled, true, 'no second upload while one is running');
  assert.equal(env.root.getAttribute('aria-busy'), 'true');
  release();
  await done;

  const [call] = env.calls;
  assert.equal(call.url, '/workspace/events/1/rooms/2/ai-vocabulary/suggestions?translate=1');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.headers['Content-Type'], 'application/pdf');
  assert.equal(call.init.headers['X-Source-Filename'], 'Talk%20Script.pdf');
  assert.equal(call.init.body, env.parts.file.files[0], 'the file is sent as it is');
  assert.equal(env.root.getAttribute('aria-busy'), 'false');
  assert.equal(env.parts.submit.disabled, false);

  assert.equal(env.parts.review.hidden, false);
  assert.deepEqual(env.parts.head.children.map((th) => th.textContent), ['#', 'Term', 'German', 'French', 'Found', 'Add it?']);
  assert.equal(rowElements(env).length, 3);
  assert.match(env.parts.status.textContent, /Found 3 terms/);
  assert.match(env.parts.status.textContent, /suggestions from OpenAI/);

  const [nllb, mission, diarization] = rowElements(env);
  assert.equal(input(nllb, (node) => node.dataset.field === 'source').value, 'NLLB');
  assert.equal(input(nllb, (node) => node.dataset.lang === 'de').value, 'NLLB', 'suggested translations are prefilled');
  assert.equal(input(diarization, (node) => node.dataset.lang === 'fr').value, '');
  assert.equal(input(mission, (node) => node.dataset.lang === 'fr'), undefined, 'a language already in the glossary has no input');
  assert.ok(walk(mission, (node) => node.textContent === 'Already in the glossary').length);
  assert.ok(walk(nllb, (node) => node.textContent === '4×').length, 'the occurrence count is shown');
  assert.ok(walk(nllb, (node) => node.textContent === 'Acronym').length);

  // ── Editing, accepting and rejecting ─────────────────────────────────
  type(input(nllb, (node) => node.dataset.field === 'source'), 'NLLB-200');
  type(input(nllb, (node) => node.dataset.lang === 'fr'), 'NLLB (Meta)');
  button(nllb, 'accept').handlers.click();
  assert.equal(nllb.classList.contains('is-accepted'), true);
  assert.equal(button(nllb, 'accept').getAttribute('aria-pressed'), 'true');
  assert.equal(env.parts.save.textContent, 'Add 1 accepted term');
  assert.equal(env.parts.save.disabled, false);

  button(diarization, 'reject').handlers.click();
  assert.equal(diarization.classList.contains('is-rejected'), true);
  assert.equal(input(diarization, (node) => node.dataset.lang === 'de').disabled, true, 'a rejected row cannot be edited');
  button(diarization, 'reject').handlers.click();
  assert.equal(env.state.rows[2].decision, PENDING, 'pressing reject again undoes it');
  button(diarization, 'reject').handlers.click();

  button(mission, 'keep').handlers.click();
  assert.equal(input(mission, (node) => node.dataset.lang === 'de').value, 'Mission Control');

  env.parts['accept-all'].handlers.click();
  assert.deepEqual(env.state.rows.map((row) => row.decision), [ACCEPTED, ACCEPTED, REJECTED], 'accept all keeps rejections');
  assert.equal(env.parts.save.textContent, 'Add 2 accepted terms');
}

// ── Saving only the accepted rows ────────────────────────────────────────

{
  const env = setup({
    replies: [response(200, SUGGESTIONS), response(200, { saved: 3, languages: ['de', 'fr'], redirect: '/workspace/events/1/rooms/2/?vocab_imported=3' })],
  });
  await upload(env);
  const [nllb, mission] = rowElements(env);
  type(input(mission, (node) => node.dataset.lang === 'de'), 'Missionskontrolle');
  button(nllb, 'accept').handlers.click();
  button(mission, 'accept').handlers.click();
  await env.parts.save.handlers.click();

  const save = env.calls[1];
  assert.equal(save.url, '/workspace/events/1/rooms/2/ai-vocabulary/suggestions/save');
  assert.equal(save.init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(save.init.body), {
    rows: [
      { row: 1, source_term: 'NLLB', match_type: 'exact', case_sensitive: true, translations: { de: 'NLLB', fr: 'NLLB' } },
      { row: 2, source_term: 'Mission Control', match_type: 'phrase', case_sensitive: false, translations: { de: 'Missionskontrolle' } },
    ],
  }, 'undecided rows are not sent, and edited values are');
  assert.deepEqual(env.navigations, ['/workspace/events/1/rooms/2/?vocab_imported=3'], 'the page reloads to show the new entries');
  assert.equal(env.parts.status.textContent, 'Added 3 terms to the glossary.');
  assert.equal(env.parts.review.hidden, true);
}

{
  // Server-side validation errors stay on their rows; nothing navigates away.
  const env = setup({
    replies: [
      response(200, SUGGESTIONS),
      response(422, { detail: 'Nothing was added. Fix or reject the rows marked below, then save again.', errors: [{ row: 1, language: 'de', message: "Row 1: 'NLLB' is already in the glossary for target language 'de'" }] }),
    ],
  });
  await upload(env);
  const [nllb] = rowElements(env);
  button(nllb, 'accept').handlers.click();
  await env.parts.save.handlers.click();
  assert.deepEqual(env.navigations, []);
  assert.equal(env.parts.status.dataset.tone, 'error');
  assert.match(env.parts.status.textContent, /^Nothing was added/);
  const errors = walk(nllb, (node) => node.tagName === 'UL')[0];
  assert.equal(errors.hidden, false);
  assert.deepEqual(errors.children.map((li) => li.textContent), ["Row 1: 'NLLB' is already in the glossary for target language 'de'"]);
  type(input(nllb, (node) => node.dataset.field === 'source'), 'NLLB model');
  assert.equal(errors.hidden, true, 'editing a row clears its errors');
}

{
  // Saving with nothing accepted asks for a decision instead of calling the server.
  const env = setup({ replies: [response(200, SUGGESTIONS)] });
  await upload(env);
  await env.parts.save.handlers.click();
  assert.equal(env.calls.length, 1);
  assert.equal(env.parts.status.textContent, 'Accept at least one term first.');
}

{
  // A redirect elsewhere is never followed.
  const env = setup({ replies: [response(200, SUGGESTIONS), response(200, { saved: 1, redirect: '//evil.example/' })] });
  await upload(env);
  button(rowElements(env)[0], 'accept').handlers.click();
  await env.parts.save.handlers.click();
  assert.deepEqual(env.navigations, []);
}

// ── Error and empty states ───────────────────────────────────────────────

{
  const env = setup();
  await upload(env, { name: 'deck.pptx', size: 10, type: 'application/vnd.ms-powerpoint' });
  assert.equal(env.calls.length, 0, 'unsupported files are not sent');
  assert.equal(env.parts.status.dataset.tone, 'error');
  assert.match(env.parts.status.textContent, /Save slides as PDF/);
  await upload(env, { name: 'big.pdf', size: 5 * 1024 * 1024, type: 'application/pdf' });
  assert.equal(env.calls.length, 0, 'oversized files are not sent');
  assert.equal(env.parts.status.textContent, 'Files must be 1 MB or smaller.');
}

{
  const env = setup({
    replies: [
      response(422, { detail: 'No text found in this PDF. Scanned pages are images, and VoxBento doesn\'t read text from images.' }),
      response(413, new Error('not json')),
      response(200, { ...SUGGESTIONS, suggestions: [] }),
      new TypeError('Failed to fetch'),
    ],
    translate: false,
  });
  await upload(env);
  assert.equal(env.calls[0].url.includes('translate'), false, 'translations are only asked for when chosen');
  assert.match(env.parts.status.textContent, /^No text found in this PDF/);
  assert.equal(env.parts.status.dataset.tone, 'error');
  assert.equal(env.parts.review.hidden, true);
  await upload(env);
  assert.equal(env.parts.status.textContent, 'This file is too large.');
  await upload(env);
  assert.match(env.parts.status.textContent, /^No new terms found/);
  assert.equal(env.parts.review.hidden, true, 'an empty result shows no table');
  const quiet = console.error;
  console.error = () => {};
  await upload(env);
  console.error = quiet;
  assert.match(env.parts.status.textContent, /Couldn't reach VoxBento/);
  assert.equal(env.parts.submit.disabled, false, 'a failure leaves the form usable');
}

{
  // A partly failed translation is reported, and the terms are still listed.
  const partial = { ...SUGGESTIONS, translation: { status: 'partial', service: 'OpenAI', failed_languages: ['fr'] } };
  const env = setup({ replies: [response(200, partial)] });
  await upload(env);
  assert.equal(rowElements(env).length, 3);
  assert.equal(env.parts.status.dataset.tone, 'warning');
  assert.match(env.parts.status.textContent, /couldn't suggest translations for French/);
  env.parts.discard.handlers.click();
  assert.equal(env.parts.review.hidden, true);
  assert.equal(env.state.rows.length, 0);
  assert.equal(env.parts.file.value, '');
  assert.equal(env.parts.status.hidden, true);
}

{
  // Untrusted terms are only ever written as text.
  const hostile = { ...SUGGESTIONS, suggestions: [{ ...SUGGESTIONS.suggestions[0], source_term: '<img src=x onerror=alert(1)>' }] };
  const env = setup({ replies: [response(200, hostile)] });
  await upload(env);
  const tr = rowElements(env)[0];
  assert.equal(input(tr, (node) => node.dataset.field === 'source').value, '<img src=x onerror=alert(1)>');
  assert.equal(walk(tr, (node) => 'innerHTML' in node).length, 0);
}

assert.equal(initGlossarySuggestions(null), null, 'pages without the section are left alone');
console.log('Glossary suggestion checks passed');
