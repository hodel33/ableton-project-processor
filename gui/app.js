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

// ════════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════════

const state = {
  pipeline: [],       // [{id, label, description, enabled}]
  settings: {},       // {key: string}
  prefixes: [],       // [{prefix, sort, color, category, comment}]
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
  state.als_files    = initial.als_files    || [];
  state.project_root = initial.project_root || '';

  renderAll();
  wireGlobalEvents();
}

function renderAll() {
  renderProjectRoot();
  renderAlsCount();
  renderFilesBar();
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
  const n = state.als_files.length;
  if (n === 0) {
    target.textContent = 'No .als files found one folder below.';
    target.classList.remove('has-files');
  } else {
    target.textContent = `${n} .als project${n === 1 ? '' : 's'} detected.`;
    target.classList.add('has-files');
  }
}

function renderFilesBar() {
  const bar = $('#filesBar');
  bar.textContent = '';
  for (const f of state.als_files) {
    const chip = el('span', { class: 'file-chip', title: f.path },
      el('span', { class: 'folder', text: f.folder }),
      el('span', { class: 'sep', text: '/' }),
      f.name
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
  markDirty();
}

function updateActiveCount() {
  const n = state.pipeline.filter((s) => s.enabled).length;
  const badge = $('#activeCount');
  badge.textContent = `${n} active`;
  badge.classList.toggle('zero', n === 0);
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
      nameInput.addEventListener('mousedown', (e) => e.stopPropagation());
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
      commentInput.addEventListener('mousedown', (e) => e.stopPropagation());
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
      draggable: !isSpecial,
      dataset: { cat: p.category },
    },
      sortNum,
      swatch,
      nameInput,
      deleteBtn,
      commentInput
    );

    if (!isSpecial) {
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
  $('#rescan').addEventListener('click', rescan);
  $('#saveConfig').addEventListener('click', () => saveConfig(true));
  $('#runBtn').addEventListener('click', runPipeline);
  $('#stopBtn').addEventListener('click', stopPipeline);
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
      runPipeline();
    }
    if (e.key === 'Escape') {
      const picker = $('#palettePicker');
      if (picker) picker.remove();
    }
  });
}

// ────────────────────────────────────────────────────────────

async function pickFolder() {
  const res = await api('pick_folder');
  if (!res || !res.ok) return;
  state.project_root = res.project_root;
  state.als_files = res.als_files;
  renderProjectRoot();
  renderAlsCount();
  renderFilesBar();
}

async function rescan() {
  const res = await api('rescan');
  if (!res || !res.ok) return;
  state.als_files = res.als_files;
  renderAlsCount();
  renderFilesBar();
  toast(`Rescan: ${state.als_files.length} file(s) found`);
}

function setAllSteps(on) {
  state.pipeline.forEach((s) => (s.enabled = on));
  renderPipeline();
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

async function runPipeline() {
  if (state.running) return;
  if (state.dirty) {
    toast('You have unsaved changes — hit Save config first.', 'error');
    return;
  }

  const res = await api('run_pipeline');
  if (!res || !res.ok) {
    toast((res && res.error) || 'Could not start pipeline', 'error');
    return;
  }
  clearConsole();
  setRunning(true);
  setStatus('running', 'Running');
  pollLogsLoop();
}

async function stopPipeline() {
  await api('stop_pipeline');
  toast('Stop requested — will halt after current file.');
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
      if (/Validation failed|crashed|Error:/i.test(tail)) {
        setStatus('error', 'Error');
        toast('Pipeline finished with errors', 'error');
      } else if (/All done|Saved processed/i.test(tail)) {
        setStatus('done', 'Done');
        toast('Pipeline finished', 'success');
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
  if (/Saved processed|All done|✓ All done|plugins list saved/i.test(s)) return 'l-success';
  if (/^\s{2,}[a-z]/i.test(s)) return 'l-dim';
  return '';
}

function appendLog(line) {
  const consoleEl = $('#console');
  const empty = consoleEl.querySelector('.console-empty');
  if (empty) empty.remove();

  if (/^\s*-{5,}\s*$/.test(line)) return;

  const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 80;

  const sepMatch = line.match(/^\s*([═─])\1{5,}\s*$/);
  const row = sepMatch
    ? el('div', { class: 'console-rule ' + (sepMatch[1] === '═' ? 'heavy' : 'light') })
    : el('div', { class: 'console-line ' + classifyLine(line), text: line || ' ' });
  consoleEl.appendChild(row);

  if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function clearConsole() {
  $('#console').textContent = '';
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
