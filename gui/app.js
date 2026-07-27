/* ═══════════════════════════════════════════════════════════
   Ableton Project Processor — Frontend
   ═══════════════════════════════════════════════════════════ */

// Ableton Live color palette (indices 0–69).
// The processor itself uses only the index.
const ABLETON_PALETTE = [
  // Row 0
  '#fe9aaa','#fea741','#d19d3a','#f7f58c','#c1fc40','#2dfe50','#34feaf',
  '#65ffea','#90c7fc','#5c86e1','#97abfb','#d975e2','#e55ca2','#ffffff',
  // Row 1
  '#fe3e40','#f76f23','#9f7752','#fff054','#8dff79','#42c52e','#11c2b2',
  '#28e9fd','#1aa6eb','#5c86e1','#8e74e2','#ba81c7','#fe41d1','#d9d9d9',
  // Row 2
  '#e26f64','#fea67e','#d6b27f','#eeffb7','#d6e6a6','#bfd383','#a4c99a',
  '#d9fde5','#d2f3f9','#c2c9e6','#d3c4e5','#b5a1e4','#eae3e7','#b3b3b3',
  // Row 3
  '#cb9b96','#bb8862','#9f8a75','#c3be78','#a9c12f','#84b45d','#93c7c0',
  '#a5bbc9','#8facc5','#8d9ccd','#ae9fbb','#c6a9c4','#bf7a9c','#838383',
  // Row 4
  '#b53637','#ae5437','#775345','#dec633','#899b31','#57a53f','#139f91',
  '#256686','#1a3096','#3155a4','#6751ae','#a752af','#ce3571','#3f3f3f',
];

// ─── Settings schema — what to render + how to describe ─────
const SETTINGS_SCHEMA = [
  { key: 'dedupe_devices',
    label: 'Dedupe devices',
    hint: 'comma-separated, partial names ok',
    note: 'Used by: Deduplicate devices per track.',
    placeholder: 'ott, saus' },
  { key: 'exclude_conversion_types',
    label: 'Exclude conversion types',
    hint: 'RTN, MST',
    note: 'Used by: Convert Mixer Vol/Pan → Utility. Skip Return/Master.',
    placeholder: 'RTN, MST' },
  { key: 'duplicate_chain_suffix',
    label: 'Duplicate chain suffix',
    hint: 'quoted — preserves whitespace',
    note: 'Appended to duplicated chain track names.',
    placeholder: "' [chain]'" },
  { key: 'exclude_midi_prefixes',
    label: 'Exclude MIDI prefixes',
    hint: 'quantize + transpose skip-list',
    note: 'Tracks whose prefix matches are not touched.',
    placeholder: 'DRUMS,DR,FX' },
  { key: 'transpose_semitones',
    label: 'Transpose semitones',
    hint: 'integer, e.g. -12 or +7',
    note: 'Clamped to stay within MIDI 0–127.',
    placeholder: '-12' },
  { key: 'lane_height',
    label: 'Lane height',
    hint: 'multiple of 17, range 17–425',
    note: 'Applied to every track for consistent lane heights.',
    placeholder: '68' },
];

// Max backup search folders the Collect panel accepts (mirrors collect.MAX_BACKUP_LOCATIONS).
const MAX_BACKUP_LOCATIONS = 3;

// ════════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════════

const state = {
  pipeline: [],       // [{id, label, description, enabled}]
  settings: {},       // {key: string}
  prefixes: [],       // [{prefix, sort, color, category, comment}]
  collect: {},        // {enabled, versions, collect_ableton_packs, collect_m4l_devices, write_report, backup_search_locations[]}
  als_files: [],      // [{name, folder, path}]
  project_root: '',
  filter: 'all',
  running: false,
  pollTimer: null,
  apiReady: false,
  dirty: false,
};

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Tiny DOM helper — creates elements with attributes/props and children.
// Using this everywhere keeps dynamic content out of innerHTML.
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'dataset' && typeof v === 'object') Object.assign(node.dataset, v);
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k in node && typeof v !== 'string') node[k] = v;
      else node.setAttribute(k, v);
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

// ════════════════════════════════════════════════════════════
// PYWEBVIEW BRIDGE
// ════════════════════════════════════════════════════════════

function onReady() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) {
      resolve();
    } else {
      window.addEventListener('pywebviewready', resolve, { once: true });
    }
  });
}

async function api(method, ...args) {
  if (!window.pywebview || !window.pywebview.api) return null;
  return window.pywebview.api[method](...args);
}

// Themed in-app confirm for still-missing files. Python (the collect worker) shows this via
// run_js("showMissingModal(n)") and blocks on a threading.Event; whichever button the user
// clicks calls resolve_missing(true/false), which releases the worker. Count only — the exact
// files and where each was expected are already printed in full to the console just above.
function showMissingModal(count) {
  const modal = $('#missingModal');    // centering layer
  const scrim = $('#missingScrim');
  const appEl = document.getElementById('app');
  const card  = modal.querySelector('.modal-card');
  const body  = $('#missingBody');
  const n = Number(count) || 0;
  body.textContent = '';
  body.append(
    el('div', {},
      el('span', { class: 'count', text: String(n) }),
      ` file${n === 1 ? '' : 's'} could not be found and will stay OFFLINE.`),
    el('div', { class: 'hint' },
      'The exact files (and where each was expected) are listed in the console. '
      + 'Cancel to abort — set a backup search location and re-run.')
  );

  // Windows puts the affirmative button on the LEFT, macOS/Linux on the RIGHT. HTML order is
  // [Cancel][Collect anyway] (macOS); on Windows swap via `order` (group stays bottom-right).
  const win = /Windows/i.test(navigator.userAgent);
  $('#missingContinue').style.order = win ? '1' : '';
  $('#missingCancel').style.order   = win ? '2' : '';

  // Draggable by its title bar — nudge it off the console on any screen size / window.
  card.style.transform = '';                        // reset to centered on each show
  const stopDrag = makeDraggable(card, $('#missingTitle'));

  const finish = (ok) => {
    document.removeEventListener('keydown', onKey);
    stopDrag();
    modal.classList.remove('show');
    scrim.classList.remove('show');
    appEl.classList.remove('spotlight');
    api('resolve_missing', ok);
  };
  const onKey = (e) => {
    if (e.key === 'Escape') finish(false);
    else if (e.key === 'Enter') finish(true);
  };
  $('#missingContinue').onclick = () => finish(true);
  $('#missingCancel').onclick   = () => finish(false);
  document.addEventListener('keydown', onKey);

  appEl.classList.add('spotlight');   // lift the console above the scrim
  scrim.classList.add('show');
  modal.classList.add('show');
  $('#missingContinue').focus();
}

// Drag `card` by `handle`; returns a cleanup fn. Offset accumulates within a single showing.
function makeDraggable(card, handle) {
  let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
  const down = (e) => {
    dragging = true; sx = e.clientX; sy = e.clientY;
    card.style.transition = 'none';
    e.preventDefault();
  };
  const move = (e) => {
    if (!dragging) return;
    card.style.transform = `translate(${ox + e.clientX - sx}px, ${oy + e.clientY - sy}px)`;
  };
  const up = (e) => {
    if (!dragging) return;
    dragging = false;
    ox += e.clientX - sx; oy += e.clientY - sy;
    card.style.transition = '';
  };
  handle.addEventListener('mousedown', down);
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
  return () => {
    handle.removeEventListener('mousedown', down);
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
  };
}

// ════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════

async function init() {
  await onReady();
  state.apiReady = true;

  const initial = await api('get_initial_state');
  if (!initial || !initial.ok) {
    toast((initial && initial.error) || 'Failed to load config.ini', 'error');
    return;
  }
  state.pipeline     = initial.pipeline     || [];
  state.settings     = initial.settings     || {};
  state.prefixes     = initial.prefixes     || [];
  state.collect      = initial.collect      || {};
  state.als_files    = initial.als_files    || [];
  state.project_root = initial.project_root || '';

  renderAll();
  wireGlobalEvents();
}

function renderAll() {
  renderProjectRoot();
  renderAlsCount();
  renderFilesBar();
  renderCollect();
  renderPipeline();
  renderSettings();
  renderPrefixes();
  renderPalette();
}

// ════════════════════════════════════════════════════════════
// RENDER — TOP BAR
// ════════════════════════════════════════════════════════════

function renderProjectRoot() {
  const target = $('#projectRoot');
  target.textContent = state.project_root || '(not selected)';
  target.title       = state.project_root || '';
}

function renderAlsCount() {
  const target = $('#alsCount');
  target.textContent = '';
  const n = state.als_files.length;
  if (n === 0) {
    target.textContent = 'No .als files found one folder below.';
    target.classList.remove('has-files');
    return;
  }
  target.classList.add('has-files');
  target.appendChild(document.createTextNode(
    `${n} .als project${n === 1 ? '' : 's'} detected.`));
  // Older sets can't be processed or collected — call out how many, in amber, so the count
  // isn't misleadingly all-clear. Names only the versions actually present (never a guessed
  // range); the per-file badges below say exactly which.
  const badFiles = state.als_files.filter(f => f.unsupported);
  if (badFiles.length) {
    const vs = [...new Set(badFiles.map(f => f.live))].sort((a, b) => a - b);
    const vtxt = vs.length === 1
      ? `Live ${vs[0]}`
      : `Live ${vs.slice(0, -1).join(', ')} & ${vs[vs.length - 1]}`;
    target.appendChild(el('span', { class: 'unsupported-note',
      text: `  ${badFiles.length} unsupported (${vtxt})` }));
  }
}

function renderFilesBar() {
  const bar = $('#filesBar');
  bar.textContent = '';
  // Supported files first, unsupported (pre-Live 11) grouped below them. Stable sort over the
  // already-alphabetical list, so each group stays A–Z.
  const files = [...state.als_files].sort((a, b) =>
    (a.unsupported ? 1 : 0) - (b.unsupported ? 1 : 0));
  for (const f of files) {
    const bad = !!f.unsupported;
    const chip = el('span', {
        class: 'file-chip' + (bad ? ' unsupported' : ''),
        title: bad
          ? `${f.path}\nSaved by Ableton Live ${f.live} — this tool supports Live 11 & 12 only.`
            + `\nOpen and re-save it in Live 11+ to include it.`
          : f.path
      },
      el('span', { class: 'folder', text: f.folder }),
      el('span', { class: 'sep', text: '/' }),
      f.name,
      bad ? el('span', { class: 'ver-badge', text: `Live ${f.live}` }) : null
    );
    bar.appendChild(chip);
  }
}

// ════════════════════════════════════════════════════════════
// RENDER — PIPELINE
// ════════════════════════════════════════════════════════════

function renderPipeline() {
  const container = $('#steps');
  container.textContent = '';
  state.pipeline.forEach((step, i) => {
    const row = el('div', {
        class: 'step' + (step.enabled ? ' on' : ''),
        dataset: { id: step.id },
        onclick: () => toggleStep(step.id),
      },
      el('div', { class: 'step-num', text: String(i + 1).padStart(2, '0') }),
      el('div', { class: 'step-body' },
        el('p', { class: 'step-label', text: step.label }),
        el('p', { class: 'step-desc', text: step.description, title: step.description })
      ),
      el('div', { class: 'switch' })
    );
    container.appendChild(row);
  });
  updateActiveCount();
}

function toggleStep(id) {
  const step = state.pipeline.find((s) => s.id === id);
  if (!step) return;
  step.enabled = !step.enabled;
  renderPipeline();
  if (id === 'sort_color_tracks') updatePrefixesLock();   // gates the Track Prefixes panel
  markDirty();
}

function updateActiveCount() {
  const n = state.pipeline.filter((s) => s.enabled).length;
  const badge = $('#activeCount');
  badge.textContent = `${n} active`;
  badge.classList.toggle('zero', n === 0);
}

// ════════════════════════════════════════════════════════════
// RENDER — COLLECT  (a mode: when enabled it replaces the pipeline)
// ════════════════════════════════════════════════════════════

const COLLECT_VERSIONS = ['1', '3', '5', 'all'];

// One toggle row, reusing the pipeline .step look (empty num cell keeps the grid).
function collectToggleRow(key, label, desc) {
  const on = !!state.collect[key];
  return el('div', {
      class: 'step' + (on ? ' on' : ''),
      onclick: () => { state.collect[key] = !state.collect[key]; renderCollect(); markDirty(); },
    },
    el('div', { class: 'step-num' }),
    el('div', { class: 'step-body' },
      el('p', { class: 'step-label', text: label }),
      el('p', { class: 'step-desc', text: desc, title: desc })
    ),
    el('div', { class: 'switch' })
  );
}

function renderCollect() {
  const enabled = !!state.collect.enabled;

  // Header enable toggle
  const head = $('#collectEnable');
  head.classList.toggle('on', enabled);
  head.querySelector('.head-switch-label').textContent = enabled ? 'On' : 'Off';

  // Versions segmented control
  const seg = el('div', { class: 'seg' });
  const current = String(state.collect.versions || '1');
  for (const v of COLLECT_VERSIONS) {
    seg.appendChild(el('button', {
      class: 'seg-btn' + (current === v ? ' active' : ''),
      type: 'button',
      text: v === 'all' ? 'All' : v,
      onclick: () => { state.collect.versions = v; renderCollect(); markDirty(); },
    }));
  }

  const body = $('#collectBody');
  body.textContent = '';
  body.append(
    el('div', { class: 'field' },
      el('label', {}, 'Versions per project ', el('span', { class: 'hint', text: 'newest first, by last-saved date' })),
      seg,
      el('div', { class: 'field-note', text: 'Only applies to a saved Ableton Project (a folder). A standalone .als is always collected on its own.' })
    ),
    collectToggleRow('collect_ableton_packs', 'Include Ableton Pack content',
      'Copy Ableton Core Library + Add-On Pack samples in, so the bundle needs no Ableton Packs installed elsewhere'),
    collectToggleRow('collect_m4l_devices', 'Include Max for Live devices',
      'Copy the .amxd devices into the bundle. Turn off if the target machine already has your M4L devices — the report lists them'),
    collectToggleRow('write_report', 'Write requirements report',
      'Add "@ Required Plugins & Packs.txt" listing external plugins, M4L devices & Ableton Packs the set needs'),
    buildBackupField()
  );

  applyCollectMode();
}

// Collect and Process are mutually exclusive: enabling Collect greys the pipeline +
// settings panels and flips the Process button so it's obvious which one fires.
function applyCollectMode() {
  const on = !!state.collect.enabled;

  // When off, collapse the panel to just its header (down to the divider) — nothing below
  // is relevant unless you're collecting, and it keeps the left column clean.
  $('#panel-collect').classList.toggle('collapsed', !on);

  $('#panel-pipeline').classList.toggle('disabled', on);
  $('#panel-settings').classList.toggle('disabled', on);
  updatePrefixesLock();

  const label = $('#runLabel');
  if (label) label.textContent = on ? 'Collect' : 'Process';
  const runBtn = $('#runBtn');
  if (runBtn) runBtn.title = on ? 'Start collecting' : 'Start processing';

  renderConsoleEmpty();   // the idle console hint differs per mode
}

// Track Prefixes only matters when Sort & Recolor will actually run — so the panel is
// usable ONLY when that step is enabled AND Collect mode is off (Collect skips the whole
// pipeline). Disabled (greyed, like Pipeline/Settings) in every other case.
function updatePrefixesLock() {
  const sortOn = state.pipeline.some((s) => s.id === 'sort_color_tracks' && s.enabled);
  $('#panel-prefixes').classList.toggle('disabled', !!state.collect.enabled || !sortOn);
}

function toggleCollectEnabled() {
  state.collect.enabled = !state.collect.enabled;
  renderCollect();
  markDirty();
  rescan(true);   // the visible file set differs by mode — refresh it silently
}

// ── Backup search locations (1–3 folders) ────────────────────
// One persistent input+Browse row that ADDS to a list; added folders show as compact chips
// with an ✕. Capped at MAX_BACKUP_LOCATIONS — once full the add row is replaced by a hint.
// The in-progress typed text lives here (not in state) so it survives re-renders but never
// gets saved to config until committed as a chip.
let backupDraft = '';

function buildBackupField() {
  const locs = state.collect.backup_search_locations || (state.collect.backup_search_locations = []);
  const full = locs.length >= MAX_BACKUP_LOCATIONS;

  const chips = locs.map((p, i) =>
    el('div', { class: 'backup-chip', title: p },
      el('span', { class: 'ico', text: '📁' }),
      el('span', { class: 'path', text: p }),
      el('button', { class: 'chip-x', type: 'button', title: 'Remove', text: '✕',
        onclick: () => removeBackupLocation(i) })
    ));

  const input = el('input', {
    type: 'text',
    value: backupDraft,
    placeholder: locs.length ? 'add another folder…' : '(optional) e.g. C:/Samples',
    spellcheck: 'false',
    oninput: (e) => { backupDraft = e.target.value; },
    onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); addBackupLocation(e.target.value); } },
  });
  const browseBtn = el('button', { class: 'btn-mini', type: 'button', text: 'Browse', onclick: pickBackupFolder });

  return el('div', { class: 'field' },
    el('label', {}, 'Backup search locations ',
      el('span', { class: 'hint', text: locs.length ? `${locs.length} / ${MAX_BACKUP_LOCATIONS}` : 'optional fallback' })),
    locs.length ? el('div', { class: 'backup-chips' }, chips) : null,
    full
      ? el('div', { class: 'field-note', text: `Maximum ${MAX_BACKUP_LOCATIONS} — remove one to add another.` })
      : el('div', { class: 'backup-row' }, input, browseBtn),
    el('div', { class: 'field-note',
      text: 'Extra folders searched for still-missing samples (master library, projects folder, external drive).' })
  );
}

function addBackupLocation(path) {
  const p = (path || '').trim();
  const locs = state.collect.backup_search_locations || (state.collect.backup_search_locations = []);
  if (!p || locs.length >= MAX_BACKUP_LOCATIONS) return;
  if (locs.some(x => x.toLowerCase() === p.toLowerCase())) { toast('That folder is already in the list'); return; }
  locs.push(p);
  backupDraft = '';
  renderCollect();
  markDirty();
}

function removeBackupLocation(i) {
  (state.collect.backup_search_locations || []).splice(i, 1);
  renderCollect();
  markDirty();
}

async function pickBackupFolder() {
  const res = await api('pick_backup_folder');
  if (!res || !res.ok) return;
  addBackupLocation(res.path);
}

// ════════════════════════════════════════════════════════════
// RENDER — SETTINGS
// ════════════════════════════════════════════════════════════

function renderSettings() {
  const grid = $('#settingsGrid');
  grid.textContent = '';
  for (const def of SETTINGS_SCHEMA) {
    const val = state.settings[def.key] != null ? state.settings[def.key] : '';

    const input = el('input', {
      type: 'text',
      value: val,
      placeholder: def.placeholder,
      spellcheck: 'false',
      dataset: { key: def.key },
      oninput: () => {
        state.settings[def.key] = input.value;
        markDirty();
      },
    });

    const wrap = el('div', { class: 'field' },
      el('label', { title: def.note },
        def.label + ' ',
        el('span', { class: 'hint', text: def.hint })
      ),
      input,
      el('div', { class: 'field-note', text: def.note })
    );
    grid.appendChild(wrap);
  }
}

// ════════════════════════════════════════════════════════════
// RENDER — TRACK PREFIXES
// ════════════════════════════════════════════════════════════

// Pure name-based classification — single source of truth.
//  • DEF / RTN / MST  → special (pinned)
//  • exactly 2 chars  → individual
//  • anything else    → group
function categorize(name) {
  const n = (name || '').trim();
  if (n === 'DEF' || n === 'RTN' || n === 'MST') return 'special';
  if (n.length === 2) return 'individual';
  return 'group';
}

function renderPrefixes() {
  const container = $('#prefixes');
  container.textContent = '';

  // Specials (Return, Master, default/missing) are pinned at 99
  state.prefixes.forEach((p) => { if (p.category === 'special') p.sort = 99; });

  // Display in ascending sort-number order (mirrors Ableton's track order)
  state.prefixes.sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0));

  state.prefixes.forEach((p) => {
    const isSpecial = p.category === 'special';
    const swatchColor = ABLETON_PALETTE[p.color] || '#888';

    const sortNum = el('div', {
      class: 'sort-num' + (isSpecial ? ' pinned' : ''),
      text: String(p.sort),
      title: isSpecial
        ? 'Pinned — Special prefixes always sort last (99)'
        : 'Drag the row to reorder — number updates automatically',
    });

    const swatch = el('div', {
      class: 'swatch',
      title: `Color index ${p.color} — click to pick`,
      style: { background: swatchColor },
      dataset: { color: String(p.color) },
      onclick: () => openPalettePicker(p.prefix),
    });

    const nameInput = el('input', {
      type: 'text',
      class: 'prefix-name-input' + (isSpecial ? ' locked' : ''),
      value: p.prefix,
      readOnly: isSpecial,
      placeholder: 'NAME',
      title: isSpecial ? 'Locked — Special prefix names are fixed' : 'Click to rename',
    });
    if (!isSpecial) {
      // Captured at render time; updated on every successful commit so we
      // can revert cleanly if the user tries an invalid rename.
      let lastCommittedName = p.prefix;

      nameInput.addEventListener('input', () => {
        p.prefix = nameInput.value;
        markDirty();
      });
      // Commit on blur (Tab / click-away / Enter / Escape all trigger this)
      nameInput.addEventListener('change', () => {
        const candidate = nameInput.value.trim();

        // Guard — reserved special names can't be hijacked by regular rows
        if (candidate === 'DEF' || candidate === 'RTN' || candidate === 'MST') {
          toast(`"${candidate}" is reserved for a special prefix — pick another name.`, 'error');
          nameInput.value = lastCommittedName;
          p.prefix = lastCommittedName;
          nameInput.focus();
          nameInput.select();
          return;
        }

        // Guard — prefix names must be unique (case-insensitive to match Ableton's matching)
        const clash = state.prefixes.find(
          (x) => x !== p && x.prefix.trim().toLowerCase() === candidate.toLowerCase()
        );
        if (clash) {
          toast(`"${candidate}" already exists — prefix names must be unique.`, 'error');
          nameInput.value = lastCommittedName;
          p.prefix = lastCommittedName;
          nameInput.focus();
          nameInput.select();
          return;
        }

        p.prefix = candidate;
        nameInput.value = candidate;
        lastCommittedName = candidate;

        const newCat = categorize(p.prefix);
        if (newCat !== p.category) {
          p.category = newCat;
          row.dataset.cat = p.category;
          // If category just flipped to/from 'special', re-render to update the lock state
          if (newCat === 'special' || isSpecial) renderPrefixes();
        }
        markDirty();
      });
      // Enter / Escape commit by blurring — native change fires automatically
      nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === 'Escape') {
          e.preventDefault();
          nameInput.blur();
        }
      });
    }

    const commentInput = el('input', {
      type: 'text',
      class: 'prefix-comment' + (isSpecial ? ' locked' : ''),
      value: p.comment || '',
      readOnly: isSpecial,
      placeholder: isSpecial
        ? 'Locked — Special prefix comments can only be edited in config.ini'
        : 'Describe this prefix — saved as # comment in config.ini',
      title: isSpecial ? 'Locked — edit this in config.ini directly' : '',
      dataset: { field: 'comment' },
    });
    if (!isSpecial) {
      commentInput.addEventListener('input', () => {
        p.comment = commentInput.value;
        markDirty();
      });
    }

    const deleteBtn = el('button', {
      type: 'button',
      class: 'delete-btn',
      title: 'Remove this prefix',
      onclick: (e) => { e.stopPropagation(); removePrefix(p); },
    });
    deleteBtn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

    const row = el('div', {
      class: 'prefix' + (isSpecial ? ' pinned' : ''),
      dataset: { cat: p.category },
    },
      sortNum,
      swatch,
      nameInput,
      deleteBtn,
      commentInput
    );

    if (!isSpecial) {
      // Only enable drag when mousedown starts outside inputs/buttons — so
      // clicking into the name or comment fields selects text normally.
      row.addEventListener('mousedown', (e) => {
        row.draggable = !e.target.closest('input, button');
      });
      row.addEventListener('dragstart', (e) => {
        row.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', p.prefix);
      });
      row.addEventListener('dragend', () => {
        row.classList.remove('dragging');
        $$('.prefix.drag-over').forEach((r) => r.classList.remove('drag-over'));
      });
      row.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        row.classList.add('drag-over');
      });
      row.addEventListener('dragleave', () => {
        row.classList.remove('drag-over');
      });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.classList.remove('drag-over');
        const sourcePrefix = e.dataTransfer.getData('text/plain');
        if (!sourcePrefix || sourcePrefix === p.prefix) return;
        reorderPrefix(sourcePrefix, p.prefix);
      });
    }

    if (state.filter !== 'all' && p.category !== state.filter) row.classList.add('hidden');
    container.appendChild(row);
  });
}

function addPrefix() {
  // Next free sort number — max of non-special sorts + 1 (specials are pinned at 99)
  const nonSpecialSorts = state.prefixes
    .filter((x) => x.category !== 'special')
    .map((x) => Number(x.sort) || 0);
  const nextSort = nonSpecialSorts.length ? Math.max(...nonSpecialSorts) + 1 : 1;

  // Unnamed rows count upward from the highest existing "NEW N" — never reused.
  const newPattern = /^NEW (\d+)$/;
  let maxNewNum = 0;
  for (const p of state.prefixes) {
    const m = newPattern.exec(p.prefix);
    if (m) maxNewNum = Math.max(maxNewNum, parseInt(m[1], 10));
  }
  const name = `NEW ${maxNewNum + 1}`;

  const randomColor = Math.floor(Math.random() * 70); // 0–69 inclusive

  const fresh = {
    prefix: name,
    sort: nextSort,
    color: randomColor,
    category: categorize(name),
    comment: '',
  };
  state.prefixes.push(fresh);

  // Switch filter to something that will show the new row
  if (state.filter !== 'all' && state.filter !== fresh.category) {
    state.filter = 'all';
    $$('#panel-prefixes .seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.filter === 'all'));
  }

  renderPrefixes();
  markDirty();

  // Focus the name input on the new row so the user can rename immediately
  const rows = $$('#prefixes .prefix');
  const target = rows.find((r) => r.querySelector('.prefix-name-input')?.value === name);
  if (target) {
    const input = target.querySelector('.prefix-name-input');
    input.focus();
    input.select();
    target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function removePrefix(p) {
  if (p.category === 'special') return; // specials are locked
  const idx = state.prefixes.indexOf(p);
  if (idx < 0) return;
  state.prefixes.splice(idx, 1);
  // Renumber non-specials 1..M; specials stay pinned at 99
  let n = 1;
  state.prefixes.forEach((x) => {
    if (x.category === 'special') x.sort = 99;
    else x.sort = n++;
  });
  renderPrefixes();
  markDirty();
}

function reorderPrefix(sourcePrefix, targetPrefix) {
  const srcIdx = state.prefixes.findIndex((x) => x.prefix === sourcePrefix);
  const tgtIdx = state.prefixes.findIndex((x) => x.prefix === targetPrefix);
  if (srcIdx < 0 || tgtIdx < 0) return;
  const src = state.prefixes[srcIdx];
  const tgt = state.prefixes[tgtIdx];
  if (src.category === 'special' || tgt.category === 'special') return;
  const [moved] = state.prefixes.splice(srcIdx, 1);
  state.prefixes.splice(tgtIdx, 0, moved);
  // Renumber non-specials 1..M; specials stay pinned at 99
  let n = 1;
  state.prefixes.forEach((p) => {
    if (p.category === 'special') p.sort = 99;
    else p.sort = n++;
  });
  renderPrefixes();
  markDirty();
}

function renderPalette() {
  const palette = $('#palette');
  palette.textContent = '';
  ABLETON_PALETTE.forEach((hex, i) => {
    palette.appendChild(el('div', {
      class: 'chip',
      dataset: { idx: String(i) },
      style: { background: hex },
      title: `Index ${i} — ${hex}`,
    }));
  });
}

// ── Palette picker overlay (shared) ─────────────────────────
function openPalettePicker(prefix) {
  const row = state.prefixes.find((x) => x.prefix === prefix);
  if (!row) return;

  const existing = $('#palettePicker');
  if (existing) existing.remove();

  const grid = el('div', {
    id: 'pickerGrid',
    style: { display: 'grid', gridTemplateColumns: 'repeat(14, 1fr)', gap: '4px' },
  });

  ABLETON_PALETTE.forEach((hex, i) => {
    const chip = el('div', {
      title: `${i} - ${hex}`,
      style: {
        aspectRatio: '1 / 1',
        borderRadius: '4px',
        cursor: 'pointer',
        background: hex,
        boxShadow: i === row.color ? 'inset 0 0 0 2px var(--amber-hi)' : 'inset 0 0 0 1px rgba(255,255,255,0.1)',
        transition: 'transform .1s',
      },
      onmouseenter: () => { chip.style.transform = 'scale(1.2)'; },
      onmouseleave: () => { chip.style.transform = ''; },
      onclick: () => {
        row.color = i;
        overlay.remove();
        renderPrefixes();
        markDirty();
      },
    });
    grid.appendChild(chip);
  });

  const closeBtn = el('button', { class: 'btn-mini', text: 'Close', onclick: () => overlay.remove() });

  const valueSpanStyle = {
    color: 'var(--amber-hi)',
    fontFamily: 'var(--font-mono)',
    fontSize: '14px',
    fontWeight: 500,
    letterSpacing: 0,
    textTransform: 'none',
    marginLeft: '10px',
  };

  const currentSpan = el('span', { text: String(row.color), style: valueSpanStyle });
  const prefixSpan  = el('span', { text: prefix,            style: valueSpanStyle });

  const labelStyle = {
    margin: 0,
    fontSize: '12px',
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: 'var(--ink-0)',
    fontWeight: 600,
  };

  const box = el('div', {
    style: {
      background: 'var(--bg-2)',
      border: '1px solid var(--stroke-hi)',
      borderRadius: '14px',
      padding: '22px',
      boxShadow: 'var(--shadow-lg)',
      maxWidth: '560px',
      width: '80%',
    },
  },
    el('div', {
      style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' },
    },
      el('h3', { style: labelStyle }, 'Choose color for', prefixSpan),
      closeBtn
    ),
    grid,
    el('p', { style: { ...labelStyle, marginTop: '14px', color: 'var(--ink-3)' } },
      'Current index', currentSpan
    )
  );

  const overlay = el('div', {
    id: 'palettePicker',
    style: {
      position: 'fixed',
      inset: 0,
      background: 'rgba(0,0,0,0.5)',
      backdropFilter: 'blur(6px)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      zIndex: 50,
    },
    onclick: (e) => { if (e.target === overlay) overlay.remove(); },
  }, box);

  document.body.appendChild(overlay);
}

// ════════════════════════════════════════════════════════════
// GLOBAL EVENT WIRING
// ════════════════════════════════════════════════════════════

function wireGlobalEvents() {
  $('#pickFolder').addEventListener('click', pickFolder);
  $('#rescan').addEventListener('click', () => rescan());
  $('#saveConfig').addEventListener('click', () => saveConfig(true));
  $('#runBtn').addEventListener('click', runActive);
  $('#stopBtn').addEventListener('click', stopPipeline);

  const collectToggle = $('#collectEnable');
  collectToggle.addEventListener('click', toggleCollectEnabled);
  collectToggle.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollectEnabled(); }
  });
  $('#clearLog').addEventListener('click', clearConsole);
  $('#allOn').addEventListener('click', () => setAllSteps(true));
  $('#allOff').addEventListener('click', () => setAllSteps(false));
  $('#addPrefix').addEventListener('click', addPrefix);

  $$('#panel-prefixes .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filter = btn.dataset.filter;
      $$('#panel-prefixes .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      renderPrefixes();
    });
  });

  // Keyboard
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveConfig(true);
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      runActive();
    }
    if (e.key === 'Escape') {
      const picker = $('#palettePicker');
      if (picker) picker.remove();
    }
  });
}

// ────────────────────────────────────────────────────────────

async function pickFolder() {
  const res = await api('pick_folder', !!state.collect.enabled);
  if (!res || !res.ok) return;
  state.project_root = res.project_root;
  state.als_files = res.als_files;
  renderProjectRoot();
  renderAlsCount();
  renderFilesBar();
}

// collect mode scans a different set (no _collected/_processed), so the list must be
// re-fetched whenever the mode flips — `silent` skips the toast for that background refresh.
async function rescan(silent) {
  const res = await api('rescan', !!state.collect.enabled);
  if (!res || !res.ok) return;
  state.als_files = res.als_files;
  renderAlsCount();
  renderFilesBar();
  if (!silent) toast(`Rescan: ${state.als_files.length} file(s) found`);
}

function setAllSteps(on) {
  state.pipeline.forEach((s) => (s.enabled = on));
  renderPipeline();
  updatePrefixesLock();   // All/None may flip Sort & Recolor, which gates Track Prefixes
  markDirty();
}

// ════════════════════════════════════════════════════════════
// CONFIG SAVE (explicit only — no auto-save)
// ════════════════════════════════════════════════════════════

function markDirty() {
  updateActiveCount();
  state.dirty = true;
  const btn = $('#saveConfig');
  if (btn) btn.classList.add('dirty');
}

function clearDirty() {
  state.dirty = false;
  const btn = $('#saveConfig');
  if (btn) btn.classList.remove('dirty');
}

async function saveConfig(explicit) {
  if (!state.apiReady) return;
  const payload = {
    pipeline: state.pipeline.map((s) => ({ id: s.id, enabled: s.enabled })),
    settings: state.settings,
    prefixes: state.prefixes.map((p) => ({ prefix: p.prefix, sort: p.sort, color: p.color, comment: p.comment || '' })),
    collect: {
      enabled:                !!state.collect.enabled,
      versions:               String(state.collect.versions || '1'),
      collect_ableton_packs:  !!state.collect.collect_ableton_packs,
      collect_m4l_devices:    !!state.collect.collect_m4l_devices,
      write_report:           !!state.collect.write_report,
      backup_search_locations: state.collect.backup_search_locations || [],
    },
  };
  const res = await api('save_config', payload);
  if (!res || !res.ok) {
    toast((res && res.error) || 'Failed to save config', 'error');
    return;
  }
  clearDirty();
  if (explicit) toast('Saved config.ini', 'success');
}

// ════════════════════════════════════════════════════════════
// PIPELINE RUN + LOG STREAMING
// ════════════════════════════════════════════════════════════

// Routes to whichever mode is active. Collect and Process are mutually exclusive —
// the Process button label already reflects which one this will start.
async function runActive() {
  if (state.running) return;
  if (state.dirty) {
    toast('You have unsaved changes — hit Save config first.', 'error');
    return;
  }

  const collect = !!state.collect.enabled;
  const res = await api(collect ? 'run_collect' : 'run_pipeline');
  if (!res || !res.ok) {
    toast((res && res.error) || `Could not start ${collect ? 'collect' : 'process'}`, 'error');
    return;
  }
  clearConsole();
  setRunning(true);
  setStatus('running', collect ? 'Collecting' : 'Processing');
  pollLogsLoop();
}

async function stopPipeline() {
  await api('stop_pipeline');
  toast(state.collect.enabled
    ? 'Stop requested — will halt after current project.'
    : 'Stop requested — will halt after current file.');
}

function pollLogsLoop() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const res = await api('poll_logs');
    if (!res) return;
    for (const line of res.lines) appendLog(line);
    if (!res.running && state.running) {
      setRunning(false);
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      const tail = $('#console').innerText.slice(-500);
      const mode = state.collect.enabled ? 'Collect' : 'Process';
      if (/Validation failed|crashed|Error:/i.test(tail)) {
        setStatus('error', 'Error');
        toast(`${mode} finished with errors`, 'error');
      } else if (/Stopped —|Stop requested/i.test(tail)) {
        setStatus('', 'Stopped');
        toast(`${mode} stopped`);
      } else if (/All done|Saved processed/i.test(tail)) {
        setStatus('done', 'Done');
        toast(`${mode} finished`, 'success');
      } else {
        setStatus('', 'Idle');
      }
      rescan();
    }
  }, 160);
}

function setRunning(running) {
  state.running = running;
  $('#runBtn').disabled = running;
  $('#stopBtn').disabled = !running;
}

function setStatus(cls, text) {
  const pill = $('#runStatus');
  pill.className = 'status-pill ' + (cls || '');
  pill.textContent = '';
  pill.appendChild(el('span', { class: 'pulse' }));
  pill.appendChild(document.createTextNode(text));
}

// ── Log line classifier & appender ──────────────────────────

function classifyLine(raw) {
  const s = raw.trimEnd();
  if (s.startsWith('────')) return 'l-rule';
  if (/^(═|━){3,}/.test(s)) return 'l-rule';
  if (s.startsWith('Project folder:') || s.startsWith('Found ') || s.startsWith('Active steps')) return 'l-head';
  if (/\.als\s*$/.test(s) && !s.includes('[')) return 'l-head';
  if (/Saved\s*→/.test(s)) return 'l-head';
  if (/\[✓\]/.test(s)) return 'l-step-on';
  if (/\[—\]/.test(s)) return 'l-step-off';
  if (/^\s*\[\+\]/.test(s)) return 'l-step-on';
  if (/^\s*⚠|Validation failed|Config error|Error:/i.test(s)) return 'l-error';
  if (/Warning|Note:/i.test(s)) return 'l-warn';
  if (/^\s*Tip:/.test(s)) return 'l-tip';   // actionable guidance — make it stand out
  if (/Saved processed|All done|✓ All done|plugins list saved/i.test(s)) return 'l-success';
  // A bare, non-indented title line — a collect project's folder name (which can't contain
  // ':') or a report section title — gets the same heading style as a filename. The em-dash
  // guard keeps sentence-y status lines ("Done — …", "Stop requested — …") out of it.
  if (s && !/^\s/.test(s) && !s.includes(':') && !s.includes('—')) return 'l-head';
  // Missing-files list: keep the filename bright & readable (default --ink-1); its
  // "expected at:" path line below is long/secondary, so that stays dimmed. Order matters —
  // the path line also ends in a file extension, so it must be caught first.
  if (/^\s+expected at:/.test(s)) return 'l-dim';
  if (/^\s{2,}\S.*(\([\d.]+\s?[KMGT]?B\)|\.\w{2,4})\s*$/.test(s)) return '';
  if (/^\s{2,}[a-z]/i.test(s)) return 'l-dim';
  return '';
}

// Copy-progress bar — the collect worker emits tab-tagged "§COPY§ done total speed eta label"
// lines (bytes done / bytes total / bytes-per-sec / eta-seconds / current project), plus
// "§COPY§ done" at the end. One animated bar for the whole run: it updates in place, its label
// flicks through to whichever project is copying, and it's removed when finished.
function handleCopyProgress(line) {
  const p = line.split('\t');
  const consoleEl = $('#console');
  let bar = $('#copyProgress');
  if (p[1] === 'done') { if (bar) bar.remove(); return; }

  const done = Number(p[1]), total = Number(p[2]);
  const speed = Number(p[3]), eta = Number(p[4]);
  const label = p.slice(5).join('\t');
  const frac = total > 0 ? done / total : 1;

  const empty = consoleEl.querySelector('.console-empty');
  if (empty) empty.remove();
  if (!bar) {
    bar = el('div', { id: 'copyProgress', class: 'copy-progress' },
      el('div', { class: 'cp-track' }, el('div', { class: 'cp-fill' })),
      el('div', { class: 'cp-label' }),
      el('div', { class: 'cp-stats' })
    );
    consoleEl.appendChild(bar);
  }
  bar.querySelector('.cp-fill').style.width = (frac * 100).toFixed(1) + '%';
  // Two rows so nothing gets truncated in the narrow console: the project on top,
  // the numbers underneath.
  bar.querySelector('.cp-label').textContent = `Copying ${label}`;
  bar.querySelector('.cp-stats').textContent =
    `${(frac * 100).toFixed(0)}%  ·  ${humanBytes(done)} / ${humanBytes(total)}`
    + `  ·  ${humanBytes(speed)}/s  ·  ETA ${fmtDuration(eta)}`;

  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 80;
  if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}

// Missing-file search — the collect worker emits "§SEARCH§ found total folder" while walking
// the disk (and "§SEARCH§ done" at the end). Show ONE status line with a spinner, the found/total
// tally and the current folder, removed when the search finishes. No fill bar: the search has no
// known total number of folders, so it's a spinner, not a percentage.
const SEARCH_SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
let searchSpin = 0;
function handleSearchProgress(line) {
  const p = line.split('\t');
  const consoleEl = $('#console');
  let box = $('#searchProgress');
  if (p[1] === 'done') { if (box) box.remove(); return; }

  const found = Number(p[1]), total = Number(p[2]);
  const folder = p.slice(3).join('\t');

  const empty = consoleEl.querySelector('.console-empty');
  if (empty) empty.remove();
  if (!box) {
    box = el('div', { id: 'searchProgress', class: 'copy-progress search' },
      el('div', { class: 'cp-label' })
    );
    consoleEl.appendChild(box);
  }
  const frame = SEARCH_SPIN[searchSpin++ % SEARCH_SPIN.length];
  box.querySelector('.cp-label').textContent =
    `${frame}  Searching for missing samples  ·  ${found}/${total} found  ·  ${folder}`;

  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 80;
  if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}

// m:ss, or h:mm:ss once past an hour — mirrors collect.py's _fmt_duration for the ETA.
function fmtDuration(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

function humanBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let x = n, i = 0;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return (i === 0 ? x.toFixed(0) : x.toFixed(1)) + ' ' + u[i];
}

// Tracks whether the previously rendered row was a heavy ═ rule, so a heading printed directly
// beneath one (a boxed title like the EXTERNAL PLUGINS SUMMARY) can skip l-head's dashed underline.
// That underline is only the stand-in for the hidden '----' under bare filenames; inside a ═ box
// it just doubles up with the bar below it.
let _prevHeavyRule = false;

function appendLog(line) {
  if (line.startsWith('§COPY§')) { handleCopyProgress(line); return; }
  if (line.startsWith('§SEARCH§')) { handleSearchProgress(line); return; }

  const consoleEl = $('#console');
  const empty = consoleEl.querySelector('.console-empty');
  if (empty) empty.remove();

  if (/^\s*-{5,}\s*$/.test(line)) return;

  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 80;

  const sepMatch = line.match(/^\s*([═─])\1{5,}\s*$/);
  let row;
  if (sepMatch) {
    const heavy = sepMatch[1] === '═';
    row = el('div', { class: 'console-rule ' + (heavy ? 'heavy' : 'light') });
    _prevHeavyRule = heavy;
  } else {
    let cls = classifyLine(line);
    if (cls === 'l-head' && _prevHeavyRule) cls += ' boxed';   // heading inside a ═ box
    row = el('div', { class: 'console-line ' + cls, text: line || ' ' });
    _prevHeavyRule = false;
  }
  consoleEl.appendChild(row);

  if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function clearConsole() {
  _prevHeavyRule = false;
  const consoleEl = $('#console');
  consoleEl.textContent = '';
  consoleEl.appendChild(el('div', { class: 'console-empty' }));
  renderConsoleEmpty();
}

// The idle console placeholder, worded for the active mode. No-op once a run has streamed
// output (appendLog removes the .console-empty div on the first line).
function renderConsoleEmpty() {
  const empty = $('#console .console-empty');
  if (!empty) return;
  empty.textContent = '';
  if (state.collect.enabled) {
    empty.append(
      el('p', {}, 'Pick a folder, set your Collect options and hit ', el('strong', { text: 'Collect' }), '.'),
      el('p', { class: 'muted small' },
        'Each project is gathered into a self-contained bundle with its samples & Max for Live devices. Your original ',
        el('code', { text: '.als' }), ' is never touched.')
    );
  } else {
    empty.append(
      el('p', {}, 'Pick a folder, toggle your processing steps and hit ', el('strong', { text: 'Process' }), '.'),
      el('p', { class: 'muted small' },
        'Every run creates a new ', el('code', { text: '_processed.als' }), ' — your original ', el('code', { text: '.als' }), ' is never touched.')
    );
  }
}

// ════════════════════════════════════════════════════════════
// UTILITIES
// ════════════════════════════════════════════════════════════

function clampInt(v, min, max, fallback) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

const TOAST_DURATION = 5500;
let toastTimer = null;
let toastRemaining = 0;
let toastStart = 0;
let toastKind = '';
let toastHoverWired = false;

function startToastTimer(ms) {
  const t = $('#toast');
  toastStart = Date.now();
  toastRemaining = ms;
  toastTimer = setTimeout(() => {
    t.className = 'toast ' + (toastKind || '');
    toastTimer = null;
  }, ms);
}

function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  toastKind = kind || '';
  t.className = 'toast show ' + toastKind;
  if (toastTimer) clearTimeout(toastTimer);
  startToastTimer(TOAST_DURATION);

  if (!toastHoverWired) {
    toastHoverWired = true;
    t.addEventListener('mouseenter', () => {
      if (!toastTimer) return;
      clearTimeout(toastTimer);
      toastTimer = null;
      toastRemaining -= Date.now() - toastStart;
    });
    t.addEventListener('mouseleave', () => {
      if (toastTimer || toastRemaining <= 0) return;
      if (!t.classList.contains('show')) return;
      startToastTimer(toastRemaining);
    });
  }
}

// ════════════════════════════════════════════════════════════
init();
