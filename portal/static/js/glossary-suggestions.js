// Glossary suggestions (#1172): upload a talk's script or slides, review the terms VoxBento
// finds, and add only the rows the owner accepts. Nothing is saved until "Add accepted terms".
// Terms come from an uploaded file, so they are only ever written with textContent or value.

export const PENDING = 'pending';
export const ACCEPTED = 'accepted';
export const REJECTED = 'rejected';
export const SOURCE_EXTENSIONS = ['.txt', '.md', '.markdown', '.pdf'];

const KIND_LABELS = {
  acronym: 'Acronym',
  mixed_case: 'Mixed-case name',
  name: 'Name',
  rare: 'Uncommon word',
};

const NETWORK_ERROR = "Couldn't reach VoxBento. Check your connection and try again.";

export function formatSize(bytes) {
  const megabytes = bytes / (1024 * 1024);
  return megabytes >= 1 ? `${Math.round(megabytes * 10) / 10} MB` : `${Math.max(1, Math.floor(bytes / 1024))} KB`;
}

/** A message for a file the server would refuse anyway, or '' when it is worth sending. */
export function checkSourceFile(file, maxBytes) {
  if (!file) return 'Choose a file first.';
  const name = String(file.name || '').toLowerCase();
  if (!SOURCE_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return 'Upload a plain text (.txt), Markdown (.md) or PDF (.pdf) file. Save slides as PDF first.';
  }
  if (!file.size) return 'The file is empty.';
  if (maxBytes && file.size > maxBytes) return `Files must be ${formatSize(maxBytes)} or smaller.`;
  return '';
}

/** Review rows from the server's suggestions: nothing is accepted until the owner says so. */
export function reviewRows(payload) {
  return (payload?.suggestions || []).map((suggestion, index) => ({
    row: index + 1,
    sourceTerm: String(suggestion.source_term || ''),
    kind: suggestion.kind,
    occurrences: Number(suggestion.occurrences) || 0,
    matchType: suggestion.match_type || 'phrase',
    caseSensitive: Boolean(suggestion.case_sensitive),
    inGlossary: new Set(suggestion.in_glossary || []),
    translations: { ...(suggestion.translations || {}) },
    decision: PENDING,
    errors: [],
  }));
}

/** The save request: accepted rows only, with the owner's edits, never rejected or undecided ones. */
export function acceptedPayload(rows, languages) {
  return {
    rows: rows
      .filter((row) => row.decision === ACCEPTED)
      .map((row) => ({
        row: row.row,
        source_term: row.sourceTerm,
        match_type: row.matchType,
        case_sensitive: row.caseSensitive,
        translations: Object.fromEntries(
          languages
            .filter((language) => !row.inGlossary.has(language.code))
            .map((language) => [language.code, row.translations[language.code] || '']),
        ),
      })),
  };
}

/** Names and acronyms usually stay as they are: copy the term into every empty translation. */
export function keepAsIs(row, languages) {
  languages.forEach((language) => {
    const current = row.translations[language.code] || '';
    if (!row.inGlossary.has(language.code) && !current.trim()) row.translations[language.code] = row.sourceTerm;
  });
}

/** Attach the server's per-row validation errors to their rows. */
export function applyErrors(rows, errors) {
  const byRow = new Map(rows.map((row) => [row.row, row]));
  rows.forEach((row) => { row.errors = []; });
  (errors || []).forEach((error) => {
    const row = byRow.get(error.row);
    if (row) row.errors.push(String(error.message || ''));
  });
}

export function translationNote(report, languages) {
  if (!report || !report.service) return '';
  const names = (report.failed_languages || [])
    .map((code) => languages.find((language) => language.code === code)?.name || code)
    .join(', ');
  if (report.status === 'complete') return `Translations are suggestions from ${report.service}: check them before you accept.`;
  if (report.status === 'partial') return `${report.service} couldn't suggest translations for ${names}. Fill those in yourself.`;
  if (report.status === 'failed') return `${report.service} couldn't suggest translations this time. Fill them in yourself, or try again.`;
  return '';
}

function errorMessage(data, response) {
  if (data && typeof data.detail === 'string') return data.detail;
  if (response.status === 413) return 'This file is too large.';
  if (response.status === 403) return "You don't have permission to change this room's glossary.";
  return `Something went wrong (error ${response.status}). Try again.`;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function element(tag, { className = '', text = '', attrs = {} } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
  return node;
}

// Rewriting an unchanged value would move the caret of the field being typed in.
function setValue(input, value) {
  if (input.value !== value) input.value = value;
}

/** Only same-site paths are followed, whatever the response says. */
function isLocalPath(url) {
  return typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') && !url.startsWith('/\\');
}

export function initGlossarySuggestions(root, options = {}) {
  if (!root) return null;
  const fetchImpl = options.fetch || globalThis.fetch.bind(globalThis);
  const navigate = options.navigate || ((url) => window.location.assign(url));
  const part = (name) => root.querySelector(`[data-suggest-${name}]`);
  const form = part('form');
  const fileInput = part('file');
  const translateInput = part('translate');
  const submitButton = part('submit');
  const status = part('status');
  const review = part('review');
  const head = part('head');
  const body = part('rows');
  const saveButton = part('save');
  const acceptAllButton = part('accept-all');
  const discardButton = part('discard');
  const maxBytes = Number(root.dataset.maxBytes) || 0;
  const state = { languages: [], rows: [], busy: false };

  function setStatus(message, tone = 'info') {
    status.textContent = message;
    status.dataset.tone = tone;
    status.hidden = !message;
  }

  function accepted() {
    return state.rows.filter((row) => row.decision === ACCEPTED).length;
  }

  function syncActions() {
    const count = accepted();
    saveButton.textContent = count ? `Add ${count} accepted term${count === 1 ? '' : 's'}` : 'Add accepted terms';
    saveButton.disabled = state.busy || count === 0;
    acceptAllButton.disabled = state.busy || state.rows.every((row) => row.decision !== PENDING);
    submitButton.disabled = state.busy;
    discardButton.disabled = state.busy;
  }

  function setBusy(busy) {
    state.busy = busy;
    root.setAttribute('aria-busy', String(busy));
    syncActions();
  }

  function syncRow(row) {
    const { view } = row;
    view.tr.classList.toggle('is-accepted', row.decision === ACCEPTED);
    view.tr.classList.toggle('is-rejected', row.decision === REJECTED);
    view.accept.setAttribute('aria-pressed', String(row.decision === ACCEPTED));
    view.reject.setAttribute('aria-pressed', String(row.decision === REJECTED));
    const rejected = row.decision === REJECTED;
    view.inputs.forEach((input) => { input.disabled = rejected; });
    view.keep.disabled = rejected;
    setValue(view.source, row.sourceTerm);
    view.translations.forEach((input, code) => setValue(input, row.translations[code] || ''));
    view.errors.replaceChildren(...row.errors.map((message) => element('li', { text: message })));
    view.errors.hidden = !row.errors.length;
  }

  function decide(row, decision) {
    row.decision = row.decision === decision ? PENDING : decision;
    syncRow(row);
    syncActions();
  }

  function edited(row) {
    if (row.errors.length) {
      row.errors = [];
      syncRow(row);
    }
  }

  function renderRow(row) {
    const tr = element('tr', { attrs: { 'data-row': String(row.row) } });
    const view = { tr, inputs: [], translations: new Map() };

    tr.append(element('td', { className: 'glossary-suggest-number', text: String(row.row) }));

    const termCell = element('td', { className: 'glossary-suggest-term' });
    view.source = element('input', {
      attrs: { type: 'text', 'aria-label': `Term ${row.row}`, 'data-field': 'source', maxlength: '120' },
    });
    view.source.addEventListener('input', () => {
      row.sourceTerm = view.source.value;
      edited(row);
    });
    view.inputs.push(view.source);
    view.keep = element('button', {
      className: 'btn btn-sm btn-outline glossary-suggest-keep',
      text: 'Keep as is',
      attrs: { type: 'button', 'data-action': 'keep', title: 'Use the term itself in every empty translation' },
    });
    view.keep.addEventListener('click', () => {
      keepAsIs(row, state.languages);
      edited(row);
      syncRow(row);
    });
    view.errors = element('ul', { className: 'glossary-suggest-errors' });
    termCell.append(
      view.source,
      element('span', { className: 'glossary-suggest-kind', text: KIND_LABELS[row.kind] || '' }),
      view.keep,
      view.errors,
    );
    tr.append(termCell);

    state.languages.forEach((language) => {
      const cell = element('td');
      if (row.inGlossary.has(language.code)) {
        cell.append(element('span', { className: 'glossary-suggest-in-glossary', text: 'Already in the glossary' }));
      } else {
        const input = element('input', {
          attrs: {
            type: 'text',
            'aria-label': `${language.name} for term ${row.row}`,
            'data-lang': language.code,
            maxlength: '120',
          },
        });
        input.addEventListener('input', () => {
          row.translations[language.code] = input.value;
          edited(row);
        });
        view.inputs.push(input);
        view.translations.set(language.code, input);
        cell.append(input);
      }
      tr.append(cell);
    });

    tr.append(element('td', { className: 'glossary-suggest-found', text: `${row.occurrences}×` }));

    const decisionCell = element('td');
    const decision = element('div', { className: 'glossary-suggest-decision', attrs: { role: 'group', 'aria-label': `Term ${row.row}` } });
    view.accept = element('button', { className: 'btn btn-sm btn-outline', text: 'Accept', attrs: { type: 'button', 'data-action': 'accept' } });
    view.reject = element('button', { className: 'btn btn-sm btn-outline', text: 'Reject', attrs: { type: 'button', 'data-action': 'reject' } });
    view.accept.addEventListener('click', () => decide(row, ACCEPTED));
    view.reject.addEventListener('click', () => decide(row, REJECTED));
    decision.append(view.accept, view.reject);
    decisionCell.append(decision);
    tr.append(decisionCell);

    row.view = view;
    syncRow(row);
    return tr;
  }

  function renderTable() {
    head.replaceChildren(
      element('th', { text: '#', attrs: { scope: 'col' } }),
      element('th', { text: 'Term', attrs: { scope: 'col' } }),
      ...state.languages.map((language) => element('th', { text: language.name, attrs: { scope: 'col' } })),
      element('th', { text: 'Found', attrs: { scope: 'col' } }),
      element('th', { text: 'Add it?', attrs: { scope: 'col' } }),
    );
    body.replaceChildren(...state.rows.map(renderRow));
    review.hidden = !state.rows.length;
    syncActions();
  }

  function clearReview() {
    state.rows = [];
    state.languages = [];
    head.replaceChildren();
    body.replaceChildren();
    review.hidden = true;
    syncActions();
  }

  async function findTerms(event) {
    event.preventDefault();
    if (state.busy) return;
    const file = fileInput.files && fileInput.files[0];
    const problem = checkSourceFile(file, maxBytes);
    if (problem) {
      setStatus(problem, 'error');
      return;
    }
    clearReview();
    setBusy(true);
    setStatus('Reading the file and looking for terms…');
    const url = `${root.dataset.suggestUrl}${translateInput && translateInput.checked ? '?translate=1' : ''}`;
    try {
      const response = await fetchImpl(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'Content-Type': file.type || 'application/octet-stream',
          'X-Source-Filename': encodeURIComponent(file.name),
        },
        body: file,
      });
      const data = await readJson(response);
      if (!response.ok) {
        setStatus(errorMessage(data, response), 'error');
        return;
      }
      state.languages = Array.isArray(data.languages) ? data.languages : [];
      state.rows = reviewRows(data);
      if (!state.rows.length) {
        setStatus('No new terms found. Terms already in the glossary are not suggested again.');
        return;
      }
      renderTable();
      const count = state.rows.length;
      const parts = [`Found ${count} term${count === 1 ? '' : 's'}. Accept the ones to add, edit them if needed, then select "Add accepted terms".`];
      if (data.truncated) parts.push('The file is long, so only its beginning was read.');
      const note = translationNote(data.translation, state.languages);
      if (note) parts.push(note);
      setStatus(parts.join(' '), data.translation?.status === 'failed' || data.translation?.status === 'partial' ? 'warning' : 'info');
    } catch (err) {
      console.error('Glossary suggestions failed', err);
      setStatus(NETWORK_ERROR, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (state.busy) return;
    const payload = acceptedPayload(state.rows, state.languages);
    if (!payload.rows.length) {
      setStatus('Accept at least one term first.', 'error');
      return;
    }
    setBusy(true);
    setStatus('Adding the accepted terms…');
    try {
      const response = await fetchImpl(root.dataset.saveUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await readJson(response);
      if (response.ok) {
        // The page reloads to show the new entries in the glossary list below.
        clearReview();
        setStatus(`Added ${data.saved} term${data.saved === 1 ? '' : 's'} to the glossary.`, 'success');
        if (isLocalPath(data.redirect)) navigate(data.redirect);
        return;
      }
      if (Array.isArray(data.errors)) {
        applyErrors(state.rows, data.errors);
        state.rows.forEach(syncRow);
      }
      setStatus(errorMessage(data, response), 'error');
    } catch (err) {
      console.error('Saving glossary suggestions failed', err);
      setStatus(NETWORK_ERROR, 'error');
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', findTerms);
  saveButton.addEventListener('click', save);
  // Accepts every undecided row; rows the owner rejected stay rejected.
  acceptAllButton.addEventListener('click', () => {
    state.rows
      .filter((row) => row.decision === PENDING)
      .forEach((row) => {
        row.decision = ACCEPTED;
        syncRow(row);
      });
    syncActions();
  });
  discardButton.addEventListener('click', () => {
    clearReview();
    fileInput.value = '';
    setStatus('');
  });

  root.hidden = false;
  syncActions();
  return state;
}

if (typeof document !== 'undefined') {
  initGlossarySuggestions(document.querySelector('[data-glossary-suggestions]'));
}
