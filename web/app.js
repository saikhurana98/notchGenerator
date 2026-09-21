'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';

const ROLES = [
  { key: '', label: '—' },
  { key: 'outer', label: 'Outer profile' },
  { key: 'interior', label: 'Interior profiles' },
  { key: 'bend', label: 'Bend lines' },
  { key: 'extent', label: 'Bend extents' },
];

// Only bend lines legitimately live on more than one layer: Onshape splits them UP/DOWN.
const MULTI = new Set(['bend']);

const LEGEND = {
  outer: 'Outer profile',
  interior: 'Interior',
  bend: 'Bend line',
  extent: 'Bend extent',
  notch: 'New notch',
  ghost: 'Original',
};
const DRAW_ORDER = ['', 'ghost', 'extent', 'bend', 'interior', 'outer', 'notch'];

const TOLERANCES = [
  'stitch_tol', 'snap_tol', 'sliver_tol', 'chord_tol', 'bridge_tol', 'max_stub', 'bend_zone',
];

const state = {
  sessionId: null,
  files: [],
  activeId: null,
  mode: 'split',
  view: null,
  home: null,
  hover: null,
  picked: null,
};

const $ = (id) => document.getElementById(id);
const active = () => state.files.find((f) => f.id === state.activeId) || null;

// ---------------------------------------------------------------- uploading

function wireUpload() {
  const input = $('file');
  const open = (append) => {
    input.dataset.append = append ? '1' : '';
    input.click();
  };

  $('dropzone').addEventListener('click', () => open(false));
  $('dropzone').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      open(false);
    }
  });
  $('add-files').addEventListener('click', () => open(true));
  $('add-tab').addEventListener('click', () => open(true));

  input.addEventListener('change', () => {
    if (input.files.length) upload(input.files, input.dataset.append === '1');
    input.value = '';
  });

  // Dropping anywhere adds to the open batch; the first drop starts one.
  let depth = 0;
  const overlay = $('drop-overlay');
  window.addEventListener('dragenter', (e) => {
    if (![...e.dataTransfer.types].includes('Files')) return;
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files, Boolean(state.sessionId));
  });
}

async function upload(fileList, append) {
  const chosen = [...fileList];
  busy(`Reading ${chosen.length} file${chosen.length === 1 ? '' : 's'}…`);
  const body = new FormData();
  for (const f of chosen) body.append('file', f);
  const url = append && state.sessionId
    ? `/api/upload?session=${encodeURIComponent(state.sessionId)}`
    : '/api/upload';

  try {
    const data = await request(url, { method: 'POST', body });
    if (!append || !state.sessionId) {
      state.files = [];
      state.sessionId = data.session_id;
    }
    for (const item of data.files) state.files.push(toFile(item));
    const unreadable = data.files.filter((f) => !f.ok);
    if (unreadable.length) {
      toast(unreadable.map((f) => `${f.filename}: ${f.error}`).join(' · '));
    }
    $('dropzone').hidden = true;
    $('tabbar').hidden = false;
    $('viewbar').hidden = false;
    $('add-files').disabled = false;
    $('run').disabled = false;
    renderTabs();
    select(state.files.find((f) => f.ok && f.id === data.files.find((d) => d.ok).file_id).id);
  } catch (err) {
    toast(err.message);
  } finally {
    idle();
  }
}

function toFile(item) {
  const file = {
    id: item.file_id,
    name: item.filename,
    ok: Boolean(item.ok),
    error: item.error || '',
    layers: item.layers || [],
    geometry: item.geometry || null,
    bounds: item.bounds || null,
    diagnostics: item.diagnostics || [],
    roleOf: {},
    result: null,
  };
  for (const [role, layers] of Object.entries(item.suggested_mapping || {})) {
    for (const layer of Array.isArray(layers) ? layers : layers ? [layers] : []) {
      file.roleOf[layer] = role;
    }
  }
  return file;
}

// ---------------------------------------------------------------- tabs

function renderTabs() {
  const host = $('tabs');
  host.replaceChildren();
  for (const file of state.files) {
    const tab = document.createElement('button');
    tab.className = 'tab' + (file.id === state.activeId ? ' active' : '');
    tab.title = file.name;

    const dot = document.createElement('i');
    dot.className = `dot ${statusOf(file)}`;
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = file.name.replace(/\.dxf$/i, '');

    tab.append(dot, label);
    tab.addEventListener('click', () => select(file.id));
    host.append(tab);
  }
  const readable = state.files.filter((f) => f.ok).length;
  $('docname').textContent =
    readable === state.files.length
      ? `${state.files.length} file${state.files.length === 1 ? '' : 's'}`
      : `${readable} of ${state.files.length} files readable`;
}

function statusOf(file) {
  if (!file.ok) return 'bad';
  if (!file.result) return '';
  if (!file.result.ok) return 'bad';
  return file.result.diagnostics?.some((d) => d.level === 'warn') ? 'warn' : 'ok';
}

async function select(id) {
  state.activeId = id;
  state.picked = null;
  state.hover = null;
  renderTabs();

  const file = active();
  if (!file) return;
  if (!file.ok) {
    $('tree').replaceChildren(el('p', 'empty', file.error || 'This file could not be read.'));
    $('tree-hint').hidden = true;
    clearCanvas();
    return;
  }
  if (!file.geometry) {
    busy(`Loading ${file.name}…`);
    try {
      const data = await request(`/api/geometry/${state.sessionId}/${file.id}`);
      file.geometry = data.geometry;
      file.bounds = data.bounds;
    } catch (err) {
      toast(err.message);
      return;
    } finally {
      idle();
    }
    if (file.id !== id) return; // the user moved on while this was in flight
  }
  renderTree();
  renderResult(file);
  resetView();
}

// ---------------------------------------------------------------- layer tree

function renderTree() {
  const file = active();
  const host = $('tree');
  host.replaceChildren();
  $('tree-hint').hidden = false;
  $('apply-all').hidden = state.files.filter((f) => f.ok).length < 2;

  for (const info of file.layers) {
    const role = file.roleOf[info.name] || '';
    const row = el('div', 'layer');
    row.dataset.role = role;
    row.dataset.layer = info.name;

    const contents = Object.entries(info.counts || {})
      .sort()
      .map(([type, n]) => `${n}×${type}`)
      .join(', ');

    const name = el('div', 'name', info.name);
    name.title = info.name;
    const meta = el('div', 'meta', contents);
    meta.title = contents;

    const select = document.createElement('select');
    for (const option of ROLES) {
      const o = document.createElement('option');
      o.value = option.key;
      o.textContent = option.label;
      select.append(o);
    }
    select.value = role;
    select.addEventListener('change', () => setRole(info.name, select.value));
    // The row's own hover/click handlers must not fight the dropdown.
    select.addEventListener('mousedown', (e) => e.stopPropagation());

    row.append(el('i', 'swatch'), name, meta, select);
    row.addEventListener('mouseenter', () => preview(info.name, false));
    row.addEventListener('mouseleave', () => preview(null, false));
    row.addEventListener('click', () => {
      state.picked = state.picked === info.name ? null : info.name;
      renderTree();
      preview(state.picked, true);
    });
    if (state.picked === info.name) row.classList.add('selected');
    host.append(row);
  }
}

function setRole(layer, role) {
  const file = active();
  if (role && !MULTI.has(role)) {
    // Outer, interior and extent each name exactly one layer, so pointing a role at a new
    // layer takes it off whichever layer held it before.
    for (const [name, held] of Object.entries(file.roleOf)) {
      if (held === role && name !== layer) delete file.roleOf[name];
    }
  }
  if (role) file.roleOf[layer] = role;
  else delete file.roleOf[layer];
  file.result = null;
  renderTree();
  renderTabs();
  paint();
}

function mappingOf(file) {
  const mapping = {};
  for (const [layer, role] of Object.entries(file.roleOf)) {
    if (!role) continue;
    if (MULTI.has(role)) (mapping[role] = mapping[role] || []).push(layer);
    else mapping[role] = layer;
  }
  if (Array.isArray(mapping.bend) && mapping.bend.length === 1) mapping.bend = mapping.bend[0];
  return mapping;
}

function wireApplyAll() {
  $('apply-all').addEventListener('click', () => {
    const source = active();
    if (!source) return;
    let changed = 0;
    for (const file of state.files) {
      if (file === source || !file.ok) continue;
      const names = new Set(file.layers.map((l) => l.name));
      const next = {};
      for (const [layer, role] of Object.entries(source.roleOf)) {
        if (names.has(layer)) next[layer] = role;
      }
      if (Object.keys(next).length) {
        file.roleOf = next;
        file.result = null;
        changed += 1;
      }
    }
    toast(changed ? `Mapping copied to ${changed} other file(s).` : 'No other file shares these layer names.');
    renderTabs();
  });
}

// ---------------------------------------------------------------- running

function readConfig() {
  const cfg = {
    session_id: state.sessionId,
    mapping: {},
    mappings: {},
    depth: Number($('depth').value),
    depth_from: $('depth-from').value,
    shape: $('shape').value,
    merge_overlapping: $('merge_overlapping').checked,
    single_layer: $('single_layer').checked,
  };
  for (const file of state.files) {
    if (file.ok) cfg.mappings[file.id] = mappingOf(file);
  }
  if ($('thickness').value !== '') cfg.thickness = Number($('thickness').value);
  for (const field of TOLERANCES) {
    if ($(field).value !== '') cfg[field] = Number($(field).value);
  }
  return cfg;
}

async function run() {
  const count = state.files.filter((f) => f.ok).length;
  busy(count > 1 ? `Notching ${count} files…` : 'Generating notches…');
  $('run').disabled = true;
  try {
    const data = await request('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readConfig()),
    });
    for (const result of data.files || []) {
      const file = state.files.find((f) => f.id === result.file_id);
      if (file) file.result = result;
    }
    renderTabs();
    renderDownload(data);
    const file = active();
    if (file) {
      renderResult(file);
      paint();
    }
    if (data.failed) {
      toast(`${data.succeeded} of ${data.succeeded + data.failed} file(s) produced a result.`);
    }
  } catch (err) {
    toast(err.message);
  } finally {
    $('run').disabled = false;
    idle();
  }
}

function renderDownload(data) {
  const link = $('download');
  const ready = state.files.filter((f) => f.result?.download_ready).length;
  link.hidden = !data.download_ready || !ready;
  if (link.hidden) return;
  link.href = `/api/download/${state.sessionId}`;
  link.setAttribute('download', data.download_name || '');
  link.textContent = ready > 1 ? `Download all (${ready})` : 'Download';
}

function renderResult(file) {
  const chip = $('status-chip');
  const result = file.result;
  chip.replaceChildren();

  if (!result) {
    chip.hidden = true;
  } else {
    chip.hidden = false;
    chip.classList.toggle('bad', !result.ok);
    const count = result.notches?.length || 0;
    const removed = (result.area_before || 0) - (result.area_after || 0);
    chip.append(
      document.createTextNode(
        result.ok
          ? count
            ? `${count} notch${count === 1 ? '' : 'es'} · ${fmt(removed)} mm² removed`
            : 'No notches were needed'
          : 'No safe result for this file',
      ),
    );
    if (result.download_ready && state.files.filter((f) => f.result?.download_ready).length > 1) {
      const one = document.createElement('a');
      one.href = `/api/download/${state.sessionId}/${file.id}`;
      one.setAttribute('download', result.download_name || '');
      one.className = 'link';
      one.style.marginLeft = '8px';
      one.textContent = 'this file';
      chip.append(one);
    }
  }
  renderDiagnostics((result || file).diagnostics);
}

function renderDiagnostics(items) {
  const host = $('diagnostics');
  host.replaceChildren();
  // Only what the user can act on. Progress notes belong in the CLI output.
  const notable = (items || []).filter((d) => d.level === 'error' || d.level === 'warn');
  notable.sort((a, b) => (a.level === b.level ? 0 : a.level === 'error' ? -1 : 1));
  $('card-diagnostics').hidden = !notable.length;
  for (const d of notable) {
    const row = el('div', `diag ${d.level}`, d.message);
    host.append(row);
  }
}

// ---------------------------------------------------------------- drawing

function roleFor(file, item) {
  // The server tags the source geometry with the mapping it guessed; the user may have
  // changed it since, so the layer is what counts here.
  return item.layer !== undefined ? file.roleOf[item.layer] || '' : item.role || '';
}

function drawInto(svg, items) {
  svg.replaceChildren();
  const root = document.createElementNS(SVG_NS, 'g');
  // DXF is y-up and SVG is y-down, so everything below stays in model coordinates.
  root.setAttribute('transform', 'scale(1,-1)');
  const sorted = [...items].sort(
    (a, b) => DRAW_ORDER.indexOf(a.role) - DRAW_ORDER.indexOf(b.role),
  );
  for (const item of sorted) {
    if (!item.pts || item.pts.length < 2) continue;
    const line = document.createElementNS(SVG_NS, 'polyline');
    line.setAttribute('points', item.pts.map(([x, y]) => `${x},${y}`).join(' '));
    line.setAttribute('vector-effect', 'non-scaling-stroke');
    line.dataset.role = item.role;
    if (item.layer) line.dataset.layer = item.layer;
    root.append(line);
  }
  svg.append(root);
}

function paint() {
  const file = active();
  if (!file || !file.geometry) return clearCanvas();

  const before = file.geometry.map((i) => ({ ...i, role: roleFor(file, i) }));
  const result = file.result;
  const after = result?.ok && result.after?.length ? result.after : null;

  let shown;
  if (state.mode === 'overlay' && after) {
    const ghost = before
      .filter((i) => i.role === 'outer' || i.role === 'interior')
      .map((i) => ({ ...i, role: 'ghost', layer: undefined }));
    shown = [...ghost, ...after];
  } else {
    shown = after || before;
  }

  drawInto($('svg-before'), before);
  drawInto($('svg-after'), shown);
  applyView();
  renderLegend(state.mode === 'split' ? [...before, ...shown] : shown);
  preview(state.hover || state.picked, state.picked && !state.hover);
}

function clearCanvas() {
  drawInto($('svg-before'), []);
  drawInto($('svg-after'), []);
  $('legend').replaceChildren();
}

function preview(layer, picked) {
  state.hover = picked ? null : layer;
  for (const svg of [$('svg-before'), $('svg-after')]) {
    svg.classList.toggle('previewing', Boolean(layer));
    for (const line of svg.querySelectorAll('polyline')) {
      const match = Boolean(layer) && line.dataset.layer === layer;
      line.classList.toggle('preview', match && !picked);
      line.classList.toggle('picked', match && Boolean(picked));
    }
  }
}

function renderLegend(items) {
  const roles = new Set(items.map((i) => i.role).filter((r) => r && LEGEND[r]));
  const host = $('legend');
  host.replaceChildren();
  for (const role of DRAW_ORDER) {
    if (!roles.has(role)) continue;
    const item = el('span');
    const swatch = el('i');
    swatch.style.color = `var(--${role})`;
    item.append(swatch, document.createTextNode(LEGEND[role]));
    host.append(item);
  }
}

// ---------------------------------------------------------------- view

function resetView() {
  const file = active();
  const b = file?.bounds || { min: [0, 0], max: [1, 1] };
  const width = Math.max(b.max[0] - b.min[0], 1e-6);
  const height = Math.max(b.max[1] - b.min[1], 1e-6);
  const pad = 0.06 * Math.max(width, height);
  // The y range is negated to match the scale(1,-1) used when drawing.
  state.home = {
    x: b.min[0] - pad,
    y: -(b.max[1] + pad),
    w: width + 2 * pad,
    h: height + 2 * pad,
  };
  state.view = { ...state.home };
  paint();
}

function applyView() {
  const v = state.view;
  if (!v) return;
  for (const svg of [$('svg-before'), $('svg-after')]) {
    const box = svg.getBoundingClientRect();
    const aspect = box.height > 0 && box.width > 0 ? box.height / box.width : 0.75;
    // Letterbox rather than distort: grow whichever dimension this panel needs.
    let { x, y, w, h } = v;
    if (h / w < aspect) {
      const grown = w * aspect;
      y -= (grown - h) / 2;
      h = grown;
    } else {
      const grown = h / aspect;
      x -= (grown - w) / 2;
      w = grown;
    }
    svg.setAttribute('viewBox', `${x} ${y} ${w} ${h}`);
  }
}

function wireView() {
  for (const button of document.querySelectorAll('.segmented button')) {
    button.addEventListener('click', () => {
      for (const other of document.querySelectorAll('.segmented button')) {
        other.classList.toggle('active', other === button);
      }
      state.mode = button.dataset.mode;
      $('views').className = `views ${state.mode}`;
      // The panels change shape between modes, so the letterboxing has to be redone.
      requestAnimationFrame(paint);
    });
  }
  $('reset-view').addEventListener('click', resetView);
  window.addEventListener('resize', applyView);

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea')) return;
    if (e.key === 'Escape' && state.picked) {
      state.picked = null;
      renderTree();
      preview(null, false);
    }
    if (e.key === 'f' || e.key === 'F') resetView();
  });

  for (const svg of [$('svg-before'), $('svg-after')]) {
    svg.addEventListener('wheel', (e) => onWheel(e, svg), { passive: false });
    svg.addEventListener('pointerdown', (e) => onDragStart(e, svg));
  }
}

function modelPoint(event, svg) {
  const box = svg.getBoundingClientRect();
  const viewBox = svg.getAttribute('viewBox');
  if (!viewBox) return null;
  const [x, y, w, h] = viewBox.split(' ').map(Number);
  return {
    x: x + ((event.clientX - box.left) / box.width) * w,
    y: y + ((event.clientY - box.top) / box.height) * h,
  };
}

function onWheel(event, svg) {
  event.preventDefault();
  if (!state.view) return;
  const at = modelPoint(event, svg);
  if (!at) return;
  const factor = Math.exp(event.deltaY * 0.0015);
  const v = state.view;
  // Zoom about the cursor, so whatever is under it stays put.
  state.view = {
    x: at.x - (at.x - v.x) * factor,
    y: at.y - (at.y - v.y) * factor,
    w: v.w * factor,
    h: v.h * factor,
  };
  applyView();
}

function onDragStart(event, svg) {
  if (event.button !== 0 || !state.view) return;
  if (!modelPoint(event, svg)) return;
  const origin = { ...state.view };
  svg.setPointerCapture(event.pointerId);
  svg.classList.add('dragging');

  const move = (e) => {
    const box = svg.getBoundingClientRect();
    const viewBox = svg.getAttribute('viewBox').split(' ').map(Number);
    const dx = ((e.clientX - event.clientX) / box.width) * viewBox[2];
    const dy = ((e.clientY - event.clientY) / box.height) * viewBox[3];
    state.view = { ...origin, x: origin.x - dx, y: origin.y - dy };
    applyView();
  };
  const stop = () => {
    svg.releasePointerCapture(event.pointerId);
    svg.classList.remove('dragging');
    svg.removeEventListener('pointermove', move);
    svg.removeEventListener('pointerup', stop);
    svg.removeEventListener('pointercancel', stop);
  };
  svg.addEventListener('pointermove', move);
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
}

// ---------------------------------------------------------------- plumbing

async function request(url, options) {
  const response = await fetch(url, options);
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* fall through to the status text below */
  }
  if (!response.ok) {
    throw new Error(detailOf(body) || `${response.status} ${response.statusText}`);
  }
  return body;
}

function detailOf(body) {
  const detail = body?.detail;
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors arrive as a list of objects.
  if (Array.isArray(detail)) return detail.map((d) => d.msg || String(d)).join('; ');
  return null;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmt(n) {
  return (n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

let toastTimer = null;
function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 7000);
}

function busy(text) {
  $('busy-text').textContent = text;
  $('busy').hidden = false;
}

function idle() {
  $('busy').hidden = true;
}

async function showBuild() {
  try {
    const info = await request('/api/version');
    if (info.version && info.version !== 'dev') {
      const chip = $('build-chip');
      chip.textContent = info.version;
      chip.title = `revision ${info.revision}`;
      chip.hidden = false;
    }
  } catch {
    /* the portal works fine without a build stamp */
  }
}

wireUpload();
wireView();
wireApplyAll();
$('run').addEventListener('click', run);
showBuild();
